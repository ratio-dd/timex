import argparse
import json
import os
from pathlib import Path

from .graph import Limits, run_council, run_single
from .models import CouncilResult, DeliberationRequest
from .time_data import load_summary


def markdown(result: CouncilResult) -> str:
    lines = ["# TimeX fake 协商结果", "", result.question, "", "仅演示合成数据和离线规则，不代表真实用户或真实模型质量。"]
    for title, positions in (("第一轮：独立立场", result.round1), ("第二轮：交叉回应", result.round2)):
        lines += ["", f"## {title}"]
        for role, position in positions.items():
            lines += ["", f"### {role}"]
            if title.startswith("第一"):
                lines += [position.position, "", *[f"- 理由：{v}" for v in position.reasons],
                          *[f"- 顾虑：{v}" for v in position.concerns],
                          *[f"- 接受条件：{v}" for v in position.acceptable_conditions],
                          f"- 证据：{', '.join(position.evidence_refs)}",
                          *[f"- 未知：{v}" for v in position.unknowns]]
            else:
                lines += [position.revised_position, "", f"- 变化说明：{position.change_explanation}"]
                for response in position.responses_to_others:
                    lines += [f"- 回应 {response.role}「{response.cited_position}」：{response.response}"]
                lines += [*[f"- 让步：{v}" for v in position.concessions],
                          *[f"- 保留异议：{v}" for v in position.remaining_objections]]
    lines += ["", "## 综合", "", *[f"- 共同点：{v}" for v in result.shared_ground]]
    if result.proposed_plan:
        lines += [result.proposed_plan.text, "", *[f"- 条件：{v}" for v in result.proposed_plan.conditions]]
    lines += [*[f"- 分歧：{v}" for v in result.unresolved_conflicts], "- 最终决定由用户作出。", "",
              *[f"- 限制：{v}" for v in result.evidence_limitations], "", "## 执行状态", "",
              f"- 状态：{result.status}", f"- 模型调用：{result.usage.model_calls}（本地 fake）",
              "- Token：未知（null）；真实模型调用：0；估算模型费用：$0。",
              f"- 耗时：{result.usage.elapsed_seconds} 秒", *[f"- 错误：{v}" for v in result.errors]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TimeX M0: offline only")
    parser.add_argument("--request", type=Path, default=Path("examples/demo_request.json"))
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--live", action="store_true", help="Unavailable in M0; always rejected")
    args = parser.parse_args(argv)
    try:
        if args.live or os.getenv("MODEL_PROVIDER", "fake") != "fake":
            raise ValueError("M0 rejects live providers")
        if args.output and args.output.exists():
            raise ValueError("output exists; choose another path to preserve it")
        # Disable opt-in tracing for this offline CLI; no raw prompts leave the process.
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
        if args.baseline:
            output = json.dumps(run_single(request), ensure_ascii=False, indent=2) + "\n"
            status = 0
        else:
            result = run_council(request, limits=Limits(max_model_calls=int(os.getenv("MAX_MODEL_CALLS", "10"))))
            output = markdown(result) if args.format == "markdown" else result.model_dump_json(indent=2) + "\n"
            status = 0 if result.status == "complete" else 1
        if args.output:
            if args.output.exists():
                raise ValueError("output exists; choose another path to preserve it")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return status
    except (ValueError, OSError):
        parser.exit(2, "Invalid request, provider, limits, CSV, or output path; inspect inputs locally.\n")


if __name__ == "__main__":
    raise SystemExit(main())
