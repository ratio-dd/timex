"""The same bounded, serial LangGraph workflow for fake and live ChatModels."""
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from .calling import BoundedCaller, error_kind
from .config import Limits, ModelSettings
from .models import (CouncilResult, DeliberationRequest, InitialPosition, ROLES,
                     RevisedPosition, SingleAdvice, Synthesis)
from .providers import make_model
from .roles import ROLE_INTERESTS


class State(TypedDict):
    round1: dict
    round2: dict
    synthesis: dict
    errors: list[str]


def caller_for(model, limits, provider, settings, live):
    if settings is not None and provider is not None and settings.provider != provider:
        raise ValueError("conflicting provider configuration")
    settings = settings or ModelSettings.from_env(provider)
    settings.authorize(live)
    limits = limits or Limits.from_env()
    return BoundedCaller(model if model is not None else make_model(settings=settings, limits=limits, live=live),
                         limits, settings)


def validate_evidence(refs, request):
    if not refs:
        raise ValueError("evidence references are required")
    for ref in refs:
        value = request
        for key in ref.split("."):
            if not isinstance(value, dict) or key not in value:
                raise ValueError("unknown evidence reference")
            value = value[key]


def validate_plan(plan, request):
    if plan.suggested_minutes > request.requested_minutes:
        raise ValueError("plan exceeds requested investment")
    if not set(request.constraints).issubset(plan.conditions):
        raise ValueError("plan omitted user constraints")


def run_council(request: DeliberationRequest, *, model: BaseChatModel | None = None,
                limits: Limits | None = None, provider: str | None = None,
                settings: ModelSettings | None = None, live: bool = False) -> CouncilResult:
    caller = caller_for(model, limits, provider, settings, live)
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

                def validate(result):
                    if role and result.suggested_minutes > request.requested_minutes:
                        raise ValueError("proposal exceeds requested investment")
                    if stage == "initial" and caller.settings.provider != "fake":
                        validate_evidence(result.evidence_refs, request_json)
                    if stage == "revise":
                        for response in result.responses_to_others:
                            if response.role == role or response.cited_position != state["round1"][response.role]["position"]:
                                raise ValueError("untraceable peer response")
                        if not result.change_explanation.strip():
                            raise ValueError("revision requires explanation")
                    if stage == "synthesis":
                        amounts = {item["suggested_minutes"] for item in state["round2"].values()}
                        objections = any(item["remaining_objections"] for item in state["round2"].values())
                        if (len(amounts) > 1 or objections) and not result.unresolved_conflicts:
                            raise ValueError("synthesis concealed disagreement")
                        validate_plan(result.proposed_plan, request)

                result = caller.invoke(payload, schema, validate)
                if stage == "initial":
                    return {"round1": {**state["round1"], role: result.model_dump()}}
                if stage == "revise":
                    return {"round2": {**state["round2"], role: result.model_dump()}}
                return {"synthesis": result.model_dump()}
            except (ValueError, TimeoutError, RuntimeError) as error:
                # Never include raw model output, HTTP bodies, or prompts in diagnostics.
                return {"errors": [f"{stage}:{role or 'coordinator'} failed: {error_kind(error)}"]}
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
                         **state["synthesis"], usage=caller.usage(),
                         status="partial" if state["errors"] else "complete", errors=state["errors"])


def run_single(request: DeliberationRequest, *, model: BaseChatModel | None = None,
               limits: Limits | None = None, provider: str | None = None,
               settings: ModelSettings | None = None, live: bool = False) -> dict:
    """One balanced assistant sees exactly the council's facts, without peer responses."""
    caller = caller_for(model, limits, provider, settings, live)
    request_json = request.model_dump(mode="json")
    advice, errors = None, []

    def validate(result):
        validate_plan(result.proposed_plan, request)
        validate_evidence(result.evidence_refs, request_json)

    try:
        advice = caller.invoke({"stage": "baseline", "interests": ROLE_INTERESTS,
                                "request": request_json}, SingleAdvice, validate).model_dump()
    except (ValueError, TimeoutError, RuntimeError) as error:
        errors = [f"baseline failed: {error_kind(error)}"]
    return {"question": request.question, "perspective": "balanced", "advice": advice,
            "usage": caller.usage().model_dump(), "status": "partial" if errors else "complete", "errors": errors,
            "limitations": "单助手同时考虑四项利益；一次案例不证明多 Agent 优于单 Agent。"}
