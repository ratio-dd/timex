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


@pytest.mark.parametrize('updates, error', [
    ({'maximum_minutes': None}, 'plan missing time semantics'),
    ({'adjustment_conditions': None}, 'plan missing time semantics'),
    ({'suggested_minutes': 60, 'maximum_minutes': 30}, 'plan ceiling below default'),
    ({'adjustment_conditions': []}, 'plan missing adjustment trigger'),
    ({'adjustment_conditions': ['  ']}, 'plan missing adjustment trigger'),
    ({'maximum_minutes': 181}, 'plan exceeds requested investment'),
])
def test_plan_default_and_conditional_ceiling_contract(updates, error):
    from timex.graph import validate_plan
    from timex.models import Plan
    request = DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())
    data = dict(text='默认暂缓；恢复后再逐日决定。', suggested_minutes=0,
                maximum_minutes=120, adjustment_conditions=['当天精力恢复且有明确空闲。'],
                conditions=request.constraints)
    validate_plan(Plan(**data), request)
    with pytest.raises(ValueError, match=error):
        validate_plan(Plan(**(data | updates)), request)


@pytest.mark.parametrize('mode', ['council', 'single'])
def test_both_paths_reject_missing_time_semantics(monkeypatch, mode):
    from timex.graph import run_single
    original = DeliberationFakeModel._generate
    def legacy(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        if json.loads(messages[-1].content)['stage'] in ('synthesis', 'baseline'):
            content = json.loads(response.generations[0].message.content)
            del content['proposed_plan']['maximum_minutes']
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(content)))])
        return response
    monkeypatch.setattr(DeliberationFakeModel, '_generate', legacy)
    request = DeliberationRequest.model_validate_json(Path('examples/evaluation/recovery.json').read_text())
    result = run_council(request).model_dump() if mode == 'council' else run_single(request)
    assert result['status'] == 'partial'
    assert result['usage']['calls'][-1]['validation_errors'][0]['code'] == 'time_semantics_required'


def test_recovery_plan_rendering_distinguishes_default_ceiling_and_legacy():
    from timex.cli import plan_lines
    from timex.models import Plan
    data = dict(text='默认暂缓', suggested_minutes=0, maximum_minutes=120,
                adjustment_conditions=['精力恢复且不影响睡眠'], conditions=['前两晚休息'])
    output = '\n'.join(plan_lines(Plan(**data)))
    assert '默认建议：0 分钟' in output
    assert '总上限：120 分钟' in output
    assert '精力恢复且不影响睡眠' in output
    legacy = Plan(text='默认暂缓，最多120分钟', suggested_minutes=120, conditions=[])
    assert '默认建议与上限未区分' in '\n'.join(plan_lines(legacy))
    assert legacy.maximum_minutes is None


def test_generation_schema_requires_current_semantics_and_exact_constraints():
    from timex.calling import messages_for
    from timex.models import Synthesis, SingleAdvice
    request = json.loads(Path('examples/evaluation/recovery.json').read_text())
    for stage, schema in [('synthesis', Synthesis), ('baseline', SingleAdvice)]:
        instruction = messages_for({'stage': stage, 'request': request}, schema)[0].content
        # The final line is the actual schema supplied to the model.
        plan = json.loads(instruction.splitlines()[-1])['$defs']['Plan']
        assert {'maximum_minutes', 'adjustment_conditions'} <= set(plan['required'])
        assert plan['properties']['maximum_minutes']['type'] == 'integer'
        assert plan['properties']['adjustment_conditions']['type'] == 'array'
        assert plan['properties']['conditions']['allOf'] == [
            {'contains': {'const': item}} for item in request['constraints']]


@pytest.mark.parametrize('mode', ['council', 'single'])
def test_confirmed_availability_caps_conditional_plan(monkeypatch, mode):
    from timex.graph import run_single
    original = DeliberationFakeModel._generate
    def speculative_capacity(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        if json.loads(messages[-1].content)['stage'] in ('synthesis', 'baseline'):
            data = json.loads(response.generations[0].message.content)
            data['proposed_plan'].update(maximum_minutes=180,
                adjustment_conditions=['如果出现额外空闲则追加'])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(data)))])
        return response
    monkeypatch.setattr(DeliberationFakeModel, '_generate', speculative_capacity)
    request = DeliberationRequest.model_validate_json(Path('examples/evaluation/conflict.json').read_text())
    result = run_council(request).model_dump() if mode == 'council' else run_single(request)
    assert result['status'] == 'partial'
    assert result['usage']['calls'][-1]['validation_errors'][0]['code'] == 'investment_exceeded'


def test_zero_confirmed_availability_is_not_treated_as_unknown():
    request = DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())
    result = run_council(request.model_copy(update={'available_minutes': 0}))
    assert result.status == 'complete'
    assert all(p.suggested_minutes == 0 for p in result.round1.values())
    assert result.proposed_plan.maximum_minutes == 0
