"""Ensure the public CI smoke check cannot echo private CLI output."""

import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "atimelogger_check", Path(__file__).parents[1] / "scripts/check_atimelogger.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_check_only_prints_safe_metadata(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ATL_TOKEN", "private-token")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    values = iter([
        (0, {"ok": True, "checks": [{"name": "auth", "ok": True, "detail": "private-token"},
                                    {"name": "private-name", "ok": True}]}),
        (0, {"seconds": 1234, "by_type": [{"type": "private-activity"}],
             "periods": [{"period": "private-date"}]}),
    ])
    monkeypatch.setattr(check, "run_cli", lambda *args: next(values))
    assert check.main() == 0
    output = capsys.readouterr().out + summary.read_text()
    assert "auth: PASS" in output
    assert "Top-level statistic groups: 1" in output
    assert "private" not in output
    assert "1234" not in output


@pytest.mark.parametrize("raw", ["private-token bad JSON", '["private-data"]'])
def test_invalid_cli_output_is_redacted(monkeypatch, raw):
    monkeypatch.setattr(check.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 1, raw, "private-error"))
    with pytest.raises(ValueError, match="private output suppressed") as error:
        check.run_cli("doctor")
    assert "private-token" not in str(error.value)
    assert "private-error" not in str(error.value)
    assert "private-data" not in str(error.value)


def test_failed_auth_stops_before_report(monkeypatch, capsys):
    monkeypatch.setenv("ATL_TOKEN", "private-token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    commands = []
    def fail(*args):
        commands.append(args)
        return 1, {"ok": False, "checks": [{"name": "auth", "ok": False}]}
    monkeypatch.setattr(check, "run_cli", fail)
    assert check.main() == 1
    assert commands == [("doctor",)]
    assert "auth: FAIL" in capsys.readouterr().out


def test_empty_report_is_successful_read():
    assert "Recorded time present: no" in check.report_lines(
        {"seconds": 0, "by_type": [], "periods": []}
    )
