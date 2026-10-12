import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from timex.graph import run_council
from timex.models import CouncilResult, DeliberationRequest
from timex.providers import DeliberationFakeModel


def test_contract_and_unknown_usage():
    request = DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())
    result = run_council(request)
    restored = CouncilResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.estimated_usd == 0
    assert result.proposed_plan.conditions[:3] == request.constraints
    assert all(v.suggested_minutes <= request.requested_minutes for v in result.round2.values())


def test_inconsistent_summary_rejected():
    data = json.loads(Path('examples/demo_request.json').read_text())
    data['time_summary']['tracked_minutes'] = 1
    with pytest.raises(ValidationError):
        DeliberationRequest.model_validate(data)


def test_fake_consensus_rejected(monkeypatch):
    original = DeliberationFakeModel._generate
    def false_consensus(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        if json.loads(messages[-1].content)['stage'] == 'synthesis':
            content = json.loads(response.generations[0].message.content)
            content['unresolved_conflicts'] = []
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(content)))])
        return response
    monkeypatch.setattr(DeliberationFakeModel, '_generate', false_consensus)
    request = DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())
    result = run_council(request)
    assert result.status == 'partial'
    assert result.proposed_plan is None


def test_cli_csv_json_and_live_rejection(tmp_path):
    output = tmp_path / 'result.json'
    process = subprocess.run([sys.executable, '-m', 'timex.cli', '--csv', 'examples/demo_events.csv',
                              '--format', 'json', '--output', str(output)], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    result = CouncilResult.model_validate_json(output.read_text())
    assert result.status == 'complete' and result.usage.model_calls == 9
    rejected = subprocess.run([sys.executable, '-m', 'timex.cli', '--live'], capture_output=True)
    assert rejected.returncode == 2
    # A second run must preserve an existing output.
    preserved = subprocess.run([sys.executable, '-m', 'timex.cli', '--output', str(output)], capture_output=True)
    assert preserved.returncode == 2
    assert CouncilResult.model_validate_json(output.read_text()) == result


def test_poc_summary_without_derived_fields_accepted():
    data = json.loads(Path('examples/demo_request.json').read_text())
    del data['time_summary']['coverage']
    del data['time_summary']['category_minutes']['untracked']
    request = DeliberationRequest.model_validate(data)
    assert request.time_summary.coverage == 2 / 3
    assert request.time_summary.category_minutes['untracked'] == 3360


def test_total_period_and_nonfinite_values_rejected():
    data = json.loads(Path('examples/demo_request.json').read_text())
    data['time_summary']['period_end'] = '2026-10-12'
    with pytest.raises(ValidationError):
        DeliberationRequest.model_validate(data)
    data = json.loads(Path('examples/demo_request.json').read_text())
    data['time_summary']['category_minutes']['sleep'] = float('nan')
    with pytest.raises(ValidationError):
        DeliberationRequest.model_validate(data)


def test_default_council_offline_even_with_tracing_enabled(monkeypatch):
    import socket
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'true')
    def no_network(*args, **kwargs):
        raise AssertionError('offline council must not open sockets')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    request = DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())
    assert run_council(request).status == 'complete'
