"""Run the author's read-only CLI without exposing account data in CI logs."""

import json
import math
import os
from pathlib import Path
import subprocess


CLI = Path(__file__).resolve().parents[1] / "tools/atimelogger/node_modules/.bin/atimelogger-cli"
CHECK_NAMES = {"token", "connection", "auth", "types"}


def run_cli(*args: str) -> tuple[int, dict]:
    child_env = os.environ.copy()
    # Node 24 fetch requires an opt-in to use the managed environment proxy.
    if child_env.get("HTTPS_PROXY") or child_env.get("HTTP_PROXY"):
        child_env.setdefault("NODE_USE_ENV_PROXY", "1")
    try:
        result = subprocess.run(
            [str(CLI), *args, "--compact"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("CLI unavailable or timed out; private output suppressed.") from None
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("CLI returned no valid JSON object; private output suppressed.") from None
    return result.returncode, value


def doctor_lines(data: dict) -> list[str]:
    """Only fixed labels and booleans may leave the private subprocess output."""
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise ValueError("Unexpected doctor response shape.")
    return [
        f"{check['name']}: {'PASS' if check.get('ok') is True else 'FAIL'}"
        for check in checks
        if isinstance(check, dict) and check.get("name") in CHECK_NAMES
    ]


def report_lines(data: dict) -> list[str]:
    seconds, groups, periods = data.get("seconds"), data.get("by_type"), data.get("periods")
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or seconds < 0
        or not isinstance(groups, list)
        or not isinstance(periods, list)
    ):
        raise ValueError("Unexpected report response shape.")
    return [
        "Last 7 days aggregate report: PASS",
        f"Top-level statistic groups: {len(groups)}",
        f"Daily buckets: {len(periods)}",
        f"Recorded time present: {'yes' if seconds > 0 else 'no'}",
    ]


def emit(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as file:
            file.write("## aTimeLogger read-only connection check\n\n")
            file.write("\n".join(f"- {line}" for line in lines) + "\n")


def main() -> int:
    lines = ["CLI: atimelogger-mcp 0.2.0 / atimelogger-cli"]
    if not os.environ.get("ATL_TOKEN"):
        emit(lines + ["Missing ATIMELOGGER_ACCESS_TOKEN in test-env."])
        return 1
    try:
        code, doctor = run_cli("doctor")
        lines.extend(doctor_lines(doctor))
        if code != 0 or doctor.get("ok") is not True:
            emit(lines + ["Stopped after doctor; no report requested."])
            return 1
        code, report = run_cli("report", "--period", "last_7_days")
        if code != 0:
            raise ValueError("Report request failed; private output suppressed.")
        lines.extend(report_lines(report))
    except ValueError as error:
        emit(lines + [str(error)])
        return 1
    emit(lines + ["Account data was not printed or uploaded. No model calls were made."])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
