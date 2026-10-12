"""Replaceable LangChain models; importing this module never calls a service."""
import json
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .models import DeliberationRequest, ROLES
from .config import Limits, ModelSettings


def initial(payload: dict[str, Any]) -> dict[str, Any]:
    request = DeliberationRequest.model_validate(payload["request"])
    role = payload["role"]
    facts = request.time_summary
    days = request.time_window_days
    sleep = facts.category_minutes.get("sleep", 0)
    low_sleep = sleep > 0 and facts.coverage >= 0.5 and sleep / days < 420
    high_work = facts.category_minutes.get("work", 0) / days > 480
    requested = request.requested_minutes
    no_career_gain = any(word in request.question for word in ("无职业收益", "无明显职业收益", "非功利"))
    if role == "career":
        amount = 0 if no_career_gain else min(requested, 60 if high_work else requested)
        reason = "议题未声称职业收益，事业立场不主张占用额外时间。" if no_career_gain else "可投入一个可撤回的小实验；这不代表已证明职业回报。"
        refs = ["question", "time_summary.category_minutes.work"]
    elif role == "curiosity":
        amount = min(requested, 120)
        reason = "探索有独立价值，先用有限时长验证兴趣，而不以收入作为唯一尺度。"
        refs = ["question", "requested_minutes"]
    elif role == "life":
        leisure = facts.category_minutes.get("leisure", 0) + facts.category_minutes.get("social", 0)
        amount = min(requested, 60 if leisure >= 120 else 30)
        reason = "保护已有生活和社交安排；记录缺口不是可任意占用的空闲。"
        refs = ["constraints", "time_summary.category_minutes.leisure", "time_summary.untracked_minutes"]
    else:
        amount = 0 if low_sleep else min(requested, 120 if sleep else 60)
        reason = ("已记录的平均睡眠低于演示规则的 7 小时参考值；优先保留恢复时间，不能据此诊断健康。"
                  if low_sleep else "没有足够证据断言身体状况，投入须以不压缩睡眠和可随时停止为条件。")
        refs = ["time_summary.category_minutes.sleep", "time_summary.coverage"]
    return {"position": f"建议新增投入 {amount} 分钟。", "suggested_minutes": amount,
            "reasons": [reason], "concerns": ["记录与议题不能证明收益或精力状况。"],
            "acceptable_conditions": [*request.constraints, "不压缩睡眠；不自动执行；用户可拒绝。"],
            "evidence_refs": refs,
            "unknowns": ["未来可用时间、实际精力与项目收益未知；合成数据不能代表用户。"]}


def revise(payload: dict[str, Any]) -> dict[str, Any]:
    role = payload["role"]
    positions = payload["round1"]
    own = positions[role]["suggested_minutes"]
    others = {key: value for key, value in positions.items() if key != role}
    # Actual peer proposals determine the revision, never a prewritten round-two script.
    caps = [value["suggested_minutes"] for value in others.values() if value["suggested_minutes"] > 0]
    revised = min([own, *caps])
    if role != "health" and positions["health"]["suggested_minutes"] == 0:
        revised = min(revised, 60)
    responses = []
    for peer, proposal in others.items():
        cap = proposal["suggested_minutes"]
        response = (f"考虑你提出的 {cap} 分钟上限，将我的建议限制到 {revised} 分钟。"
                    if 0 < cap < own else
                    f"你主张 {cap} 分钟；我保留 {revised} 分钟建议，保留分歧并交给用户裁决。" if cap != revised else
                    f"你的 {cap} 分钟建议与我的修订一致；仍需用户确认约束。")
        responses.append({"role": peer, "cited_position": proposal["position"], "response": response})
    changed = revised != own
    health_disagrees = role != "health" and positions["health"]["suggested_minutes"] == 0 and revised > 0
    return {"revised_position": f"修订后建议新增投入 {revised} 分钟。", "suggested_minutes": revised,
            "responses_to_others": responses,
            "concessions": [f"将 {own} 分钟缩减为 {revised} 分钟，以回应其他立场的时间上限。"] if changed else [],
            "remaining_objections": ["健康立场建议零新增投入；本建议不是全体共识。"] if health_disagrees else
                                    ["优先恢复，暂不接受新增投入。"] if revised == 0 else [],
            "change_explanation": "根据其他角色提出的具体时长缩小实验。" if changed else
                                  "已读取其他立场；其建议未消除我的顾虑，保留原时间主张。"}


def synthesize(payload: dict[str, Any]) -> dict[str, Any]:
    request = DeliberationRequest.model_validate(payload["request"])
    proposals = {role: payload["round2"][role]["suggested_minutes"] for role in ROLES}
    unanimous = len(set(proposals.values())) == 1
    amount = min(proposals.values())
    conflict = [] if unanimous else ["新增投入时长存在分歧：" + "；".join(f"{role}={minutes} 分钟" for role, minutes in proposals.items())]
    conflict.extend(f"{role}：{objection}" for role in ROLES
                    for objection in payload["round2"][role]["remaining_objections"])
    text = (f"各方修订后的时长一致：可考虑 {amount} 分钟，用户可拒绝。" if unanimous else
            f"未达成一致。保守候选是新增 {amount} 分钟；其他时长及健康顾虑待用户裁决。")
    return {"shared_ground": ["保留用户约束，建议不自动执行，用户可拒绝。"],
            "proposed_plan": {"text": text, "suggested_minutes": amount,
                              "conditions": [*request.constraints, "不压缩睡眠；用户确认实际精力及空闲。"]},
            "unresolved_conflicts": conflict, "requires_user_decision": True,
            "evidence_limitations": [*request.time_summary.notes,
                "fake 模型按公开演示规则生成，不证明真实模型的协商质量。",
                "记录缺口、分类偏差、未来可用时间与收益均存在不确定性。"]}


class DeliberationFakeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "timex-local-rule-fake"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        payload = json.loads(messages[-1].content)
        handlers = {"initial": initial, "revise": revise, "synthesis": synthesize, "baseline": baseline}
        output = handlers[payload["stage"]](payload)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(output, ensure_ascii=False)))])


def baseline(payload: dict[str, Any]) -> dict[str, Any]:
    positions = {role: initial({**payload, "role": role}) for role in ROLES}
    amount = min(position["suggested_minutes"] for position in positions.values())
    return {"considerations": {role: position["reasons"][0] for role, position in positions.items()},
            "proposed_plan": {"text": f"单助手综合四项利益，候选投入 {amount} 分钟；用户可拒绝。",
                              "suggested_minutes": amount, "conditions": payload["request"]["constraints"]},
            "tradeoffs": ["各利益的时间建议可能不同，较少投入会牺牲探索机会。"],
            "evidence_refs": ["question", "constraints", "time_summary"],
            "unknowns": ["fake 演示规则；无法据此评价真实模型质量。"], "requires_user_decision": True}


def make_model(provider: str | None = None, *, live: bool = False,
               settings: ModelSettings | None = None, limits: Limits | None = None) -> BaseChatModel:
    settings = settings or ModelSettings.from_env(provider)
    settings.authorize(live)
    if settings.provider == "fake":
        return DeliberationFakeModel()
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY must be configured in environment secrets")
    try:
        from langchain_openai import ChatOpenAI
        from langchain_openai.chat_models.base import BaseChatOpenAI
    except ImportError as error:
        raise ValueError("install TimeX live dependencies first") from error
    limits = limits or Limits()
    # Compatible endpoints (including DeepSeek) use max_tokens. ChatOpenAI itself
    # rewrites this to OpenAI's newer max_completion_tokens field.
    model_class = BaseChatOpenAI if settings.base_url else ChatOpenAI
    return model_class(model=settings.model_name, base_url=settings.base_url,
                      max_tokens=limits.max_output_tokens, timeout=limits.call_timeout_seconds,
                      max_retries=0, streaming=False, stream_usage=False,
                      use_responses_api=False, cache=False, http_socket_options=(),
                      extra_body={"thinking": {"type": settings.thinking}} if settings.thinking else None,
                      model_kwargs={"response_format": {"type": "json_object"}})


def retryable(error: Exception) -> bool:
    """SDK retries are disabled; only transient failures qualify for our shared retry."""
    try:
        from openai import APIConnectionError, APIStatusError, APITimeoutError
    except ImportError:
        return False
    if isinstance(error, APITimeoutError):
        return False
    return isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError) and (error.status_code == 429 or error.status_code >= 500))


def truncated_response(error: Exception):
    """The SDK's JSON parser raises on truncation but retains billable usage."""
    try:
        from openai import LengthFinishReasonError
    except ImportError:
        return None
    if not isinstance(error, LengthFinishReasonError):
        return None
    usage = error.completion.usage
    metadata = ({"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                 "total_tokens": usage.total_tokens} if usage else None)
    return AIMessage(content="", usage_metadata=metadata, response_metadata={"finish_reason": "length"})


def timed_out(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    try:
        from openai import APITimeoutError
    except ImportError:
        return False
    return isinstance(error, APITimeoutError)
