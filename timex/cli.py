import argparse
import json
import os
from pathlib import Path

from .graph import Limits, run_council, run_single
from .config import ModelSettings
from .comparison import run_comparison
from .models import CouncilResult, DeliberationRequest
from .time_data import load_summary


def markdown(result: CouncilResult) -> str:
    lines = [f"# TimeX {result.usage.provider} 协商结果", "", result.question, "",
             "离线规则演示，不代表真实模型质量。" if result.usage.provider == "fake" else
             "真实模型输出；事实范围与数据来源见输入摘要，建议由用户审阅决定。"]
    for title, positions in (("第一轮：独立立场", result.round1), ("第二轮：交叉回应", result.round2)):
        lines += ["", f"## {title}"]
        for role, position in positions.items():
            lines += ["", f"### {role}"]
            if title.startswith("第一"):
                lines += [position.position, "", f"- 建议投入：{position.suggested_minutes} 分钟", *[f"- 理由：{v}" for v in position.reasons],
                          *[f"- 顾虑：{v}" for v in position.concerns],
                          *[f"- 接受条件：{v}" for v in position.acceptable_conditions],
                          f"- 证据：{', '.join(position.evidence_refs)}",
                          *[f"- 未知：{v}" for v in position.unknowns]]
            else:
                lines += [position.revised_position, "", f"- 建议投入：{position.suggested_minutes} 分钟", f"- 变化说明：{position.change_explanation}"]
                for response in position.responses_to_others:
                    lines += [f"- 回应 {response.role}「{response.cited_position}」：{response.response}"]
                lines += [*[f"- 让步：{v}" for v in position.concessions],
                          *[f"- 保留异议：{v}" for v in position.remaining_objections]]
    lines += ["", "## 综合", "", *[f"- 共同点：{v}" for v in result.shared_ground]]
    if result.proposed_plan:
        lines += [result.proposed_plan.text, "", *[f"- 条件：{v}" for v in result.proposed_plan.conditions]]
    lines += [*[f"- 分歧：{v}" for v in result.unresolved_conflicts], "- 最终决定由用户作出。", "",
              *[f"- 限制：{v}" for v in result.evidence_limitations], "", "## 执行状态", "",
              f"- 状态：{result.status}", f"- Provider / 模型：{result.usage.provider} / {result.usage.model or 'local-rules'}",
              f"- 模型调用：{result.usage.model_calls}",
              f"- 输入 / 输出 Token：{result.usage.input_tokens} / {result.usage.output_tokens}（None 表示未知）",
              f"- 计量估算费用（USD）：{result.usage.estimated_usd}；预算占用估算：{result.usage.budget_estimated_usd}",
              f"- 耗时：{result.usage.elapsed_seconds} 秒", *[f"- 错误：{v}" for v in result.errors]]
    for call in result.usage.calls:
        for issue in call.validation_errors:
            lines.append(f"- 校验诊断（{call.stage}/{call.role}）：{issue.code} @ {issue.path}")
    return "\n".join(lines) + "\n"


def comparison_markdown(report):
    lines = ["# TimeX 多 Agent / 单 Agent 对照", "", f"模式：{report['mode']}；状态：{report['status']}",
             f"执行 ID：{report['execution_id']}；时间：{report['created_at']}", "",
             "## 相同输入（议题、分类摘要与约束）", "", "```json",
             json.dumps(report["request"], ensure_ascii=False, indent=2), "```", "",
             markdown(CouncilResult.model_validate(report["council"])), "## 单助手建议", ""]
    single = report["single"]
    if single:
        lines += ["```json", json.dumps(single, ensure_ascii=False, indent=2), "```"]
    else:
        lines += ["议会未完成，已跳过对照调用。"]
    lines += ["", "## 合计用量与时延", "", "```json",
              json.dumps(report["total_usage"], ensure_ascii=False, indent=2), "```", "",
              "## 人工评价（1–5 分，未评分）", "",
              "- 是否覆盖真正关心的利益：待评分", "- 是否回应彼此意见：待评分",
              "- 是否呈现可解释的让步或分歧：待评分", "- 是否比单助手更有帮助：待评分", "",
              *[f"- 限制：{item}" for item in report["limitations"]]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TimeX: fake by default; explicit --live for configured models")
    parser.add_argument("--request", type=Path, default=Path("examples/demo_request.json"))
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", action="store_true", help="one balanced assistant, JSON output")
    mode.add_argument("--compare", action="store_true", help="council and balanced single-assistant report")
    parser.add_argument("--live", action="store_true", help="explicitly enable the configured paid model")
    args = parser.parse_args(argv)
    try:
        settings = ModelSettings.from_env()
        settings.authorize(args.live)
        limits = Limits.from_env()
        if args.output and args.output.exists():
            raise ValueError("output exists; choose another path to preserve it")
        # Disable prompt tracing even when the model itself is explicitly live.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        data = json.loads(args.request.read_text(encoding="utf-8"))
        request = DeliberationRequest.model_validate(data)
        if args.csv:
            summary = load_summary(args.csv, period_start=request.time_summary.period_start,
                                   period_end=request.time_summary.period_end,
                                   timezone_name=request.time_summary.timezone)
            summary.notes.append("CLI 本次 CSV 输入；示例文件为合成数据，真实私有输入不得提交。")
            request = request.model_copy(update={"time_summary": summary})
        kwargs = {"settings": settings, "limits": limits, "live": args.live}
        if args.compare:
            report = run_comparison(request, **kwargs)
            output = (comparison_markdown(report) if args.format == "markdown" else
                      json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            status = 0 if report["status"] == "complete" else 1
        elif args.baseline:
            report = run_single(request, **kwargs)
            output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            status = 0 if report["status"] == "complete" else 1
        else:
            result = run_council(request, **kwargs)
            output = markdown(result) if args.format == "markdown" else result.model_dump_json(indent=2) + "\n"
            status = 0 if result.status == "complete" else 1
        if args.output:
            if args.output.exists():
                raise ValueError("output exists; choose another path to preserve it")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as destination:
                destination.write(output)
        else:
            print(output, end="")
        return status
    except (ValueError, OSError):
        parser.exit(2, "Invalid request, provider, limits, CSV, or output path; inspect inputs locally.\n")


if __name__ == "__main__":
    raise SystemExit(main())
