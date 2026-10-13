"""Small reproducible comparison artifact; no automated quality verdict."""
from datetime import datetime, timezone
import time
from uuid import uuid4

from .config import Limits, ModelSettings
from .graph import run_council, run_single


def run_comparison(request, *, model=None, limits=None, provider=None, settings=None, live=False):
    started = time.monotonic()
    if settings is not None and provider is not None and settings.provider != provider:
        raise ValueError("conflicting provider configuration")
    settings = settings or ModelSettings.from_env(provider)
    settings.authorize(live)
    limits = limits or Limits.from_env()
    kwargs = {"model": model, "limits": limits, "settings": settings, "live": live}
    council = run_council(request, **kwargs)
    # An interrupted council is a failed experiment, not a reason to issue more requests.
    single = run_single(request, **kwargs) if council.status == "complete" else None
    usages = [council.usage.model_dump()] + ([single["usage"]] if single else [])

    def total(key):
        return sum(u[key] for u in usages) if all(u[key] is not None for u in usages) else None

    return {"report_version": 2, "execution_id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "live" if live else "fake", "request": request.model_dump(mode="json"),
            "model_settings": vars(settings),
            "limits_per_run": vars(limits), "council": council.model_dump(mode="json"), "single": single,
            "status": "complete" if single and single["status"] == "complete" else "partial",
            "total_usage": {**{key: total(key) for key in ("model_calls", "input_tokens", "output_tokens",
                              "estimated_usd", "budget_estimated_usd")},
                            "elapsed_seconds": round(time.monotonic() - started, 4)},
            "manual_review": {"interest_coverage": None, "peer_responsiveness": None,
                              "explainable_concessions_or_conflicts": None, "more_helpful_than_single": None,
                              "notes": "请人工按 1–5 评分；不预设多 Agent 更好。"},
            "limitations": ["相同事实、模型与单次输出上限；多 Agent 使用更多调用和总 Token。",
                            "单案例不能证明质量优势；费用按配置单价估算，不是账单或硬封顶。",
                            "议会与单助手各自受单次预算约束；合计软预算最多为两倍。",
                            "议会不完整时跳过单助手对照。"]}
