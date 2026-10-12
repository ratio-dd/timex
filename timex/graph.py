import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from .models import (CouncilResult, DeliberationRequest, InitialPosition, ROLES,
                     RevisedPosition, Synthesis, Usage)
from .providers import make_model
from .roles import ROLE_INTERESTS


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 10
    call_timeout_seconds: float = 5
    total_timeout_seconds: float = 30
    max_input_characters: int = 30000
    max_output_characters: int = 12000

    def __post_init__(self):
        if not 1 <= self.max_model_calls <= 10:
            raise ValueError("max_model_calls must be in 1..10")
        if min(self.call_timeout_seconds, self.total_timeout_seconds,
               self.max_input_characters, self.max_output_characters) <= 0:
            raise ValueError("limits must be positive")


class BoundedCaller:
    def __init__(self, model: BaseChatModel, limits: Limits):
        self.model, self.limits = model, limits
        self.started = time.monotonic()
        self.calls = 0

    def invoke(self, payload, schema):
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > self.limits.max_input_characters:
            raise ValueError("input length limit exceeded")
        remaining = self.limits.total_timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("total timeout exceeded")
        if self.calls >= self.limits.max_model_calls:
            raise ValueError("model call budget exhausted")
        self.calls += 1
        responses = queue.Queue(maxsize=1)
        messages = [SystemMessage(content="四个利益视角的离线演示。仅使用共同事实，回复 JSON。"), HumanMessage(content=encoded)]

        def invoke():
            try:
                with tracing_context(enabled=False):
                    responses.put((True, self.model.invoke(messages, config={"callbacks": []})))
            except Exception as error:
                responses.put((False, error))

        # A timed-out invocation is abandoned; no retries or subsequent calls follow.
        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        try:
            ok, result = responses.get(timeout=min(remaining, self.limits.call_timeout_seconds))
        except queue.Empty as error:
            raise TimeoutError("model/total timeout exceeded") from error
        if not ok:
            raise RuntimeError("model invocation failed") from result
        if not isinstance(result.content, str) or len(result.content) > self.limits.max_output_characters:
            raise ValueError("output length or type limit exceeded")
        return schema.model_validate_json(result.content)


class State(TypedDict):
    round1: dict
    round2: dict
    synthesis: dict
    errors: list[str]


def run_council(request: DeliberationRequest, *, model: BaseChatModel | None = None,
                limits: Limits | None = None, provider: str | None = None) -> CouncilResult:
    provider = provider or os.getenv("MODEL_PROVIDER", "fake")
    # Reject live even when a model was injected. M0 production entry points are offline only.
    if provider != "fake":
        raise ValueError("M0 only supports fake")
    caller = BoundedCaller(model if model is not None else make_model(provider), limits or Limits())
    request_json = request.model_dump(mode="json")
    workflow = StateGraph(State)

    def node(stage, role=None):
        def execute(state):
            if state["errors"]:
                return {}
            try:
                payload = {"stage": stage, "request": request_json}
                if role:
                    payload.update(role=role, interests=ROLE_INTERESTS[role])
                if stage == "revise":
                    payload["round1"] = state["round1"]
                if stage == "synthesis":
                    payload["round2"] = state["round2"]
                schema = {"initial": InitialPosition, "revise": RevisedPosition, "synthesis": Synthesis}[stage]
                result = caller.invoke(payload, schema)
                if role and result.suggested_minutes > request.requested_minutes:
                    raise ValueError("proposal exceeds requested investment")
                if stage == "initial":
                    return {"round1": {**state["round1"], role: result.model_dump()}}
                if stage == "revise":
                    for response in result.responses_to_others:
                        if response.role == role or response.cited_position != state["round1"][response.role]["position"]:
                            raise ValueError("untraceable peer response")
                    return {"round2": {**state["round2"], role: result.model_dump()}}
                amounts = {item["suggested_minutes"] for item in state["round2"].values()}
                if len(amounts) > 1 and not result.unresolved_conflicts:
                    raise ValueError("synthesis concealed disagreement")
                if result.proposed_plan.suggested_minutes > request.requested_minutes:
                    raise ValueError("plan exceeds requested investment")
                return {"synthesis": result.model_dump()}
            except (ValueError, TimeoutError, RuntimeError, ValidationError) as error:
                # Never include raw model output or private prompts in errors/logs.
                kind = "timeout" if isinstance(error, TimeoutError) else "invocation" if isinstance(error, RuntimeError) else "contract_or_limit"
                return {"errors": [f"{stage}:{role or 'coordinator'} failed: {kind}"]}
        return execute

    previous = START
    for stage in ("initial", "revise"):
        for role in ROLES:
            name = f"{stage}_{role}"
            workflow.add_node(name, node(stage, role))
            workflow.add_edge(previous, name)
            previous = name
    workflow.add_node("synthesis", node("synthesis"))
    workflow.add_edge(previous, "synthesis")
    workflow.add_edge("synthesis", END)
    with tracing_context(enabled=False):
        state = workflow.compile().invoke({"round1": {}, "round2": {}, "synthesis": {}, "errors": []},
                                          {"recursion_limit": 12})
    return CouncilResult(question=request.question, round1=state["round1"], round2=state["round2"],
                         **state["synthesis"], usage=Usage(model_calls=caller.calls,
                         elapsed_seconds=round(time.monotonic() - caller.started, 4)),
                         status="partial" if state["errors"] else "complete", errors=state["errors"])


def run_single(request: DeliberationRequest) -> dict:
    """One-call exploration perspective baseline, explicitly narrower than the council."""
    caller = BoundedCaller(make_model(), Limits())
    position = caller.invoke({"stage": "initial", "role": "curiosity", "request": request.model_dump(mode="json")}, InitialPosition)
    return {"perspective": "curiosity", "position": position.model_dump(), "usage": Usage(model_calls=caller.calls).model_dump(),
            "limitations": "单一探索视角对照；不是无偏的真实模型质量评估。"}
