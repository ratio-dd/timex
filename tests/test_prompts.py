"""Prompt boundaries and accepted decisions; not a model-quality score."""
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from timex.comparison import run_comparison
from timex.graph import run_council
from timex.models import DeliberationRequest
from timex.providers import DeliberationFakeModel
from timex.roles import ROLE_INTERESTS


@pytest.mark.parametrize('available_minutes', [None, 60, 0])
def test_stage_prompts_preserve_snapshot_and_availability_boundaries(monkeypatch, available_minutes):
    data = json.loads(Path('examples/demo_request.json').read_text())
    data['available_minutes'] = available_minutes
    request = DeliberationRequest.model_validate(data)
    captured = []
    original = DeliberationFakeModel._generate

    def record(self, messages, **kwargs):
        captured.append(messages)
        return original(self, messages, **kwargs)

    monkeypatch.setattr(DeliberationFakeModel, '_generate', record)
    report = run_comparison(request)
    assert report['status'] == 'complete'
    assert len(captured) == report['total_usage']['model_calls'] == 10
    stages = [json.loads(messages[-1].content)['stage'] for messages in captured]
    assert stages == ['initial'] * 4 + ['revise'] * 4 + ['synthesis', 'baseline']
    ceiling = request.requested_minutes if available_minutes is None else available_minutes

    for messages in captured:
        instruction = messages[0].content
        payload = json.loads(messages[-1].content)
        stage = payload['stage']
        contract = json.loads(instruction.splitlines()[-1])
        assert payload['request'] == request.model_dump(mode='json')
        assert payload['request']['available_minutes'] == available_minutes
        assert '非功利探索不以职业收益、作品或收入为必要门槛' in instruction
        # A role must receive only its current-stage task, not the coordinator's task.
        assert f'当前阶段：{stage}。' in instruction
        assert all(f'当前阶段：{other}。' not in instruction
                   for other in set(stages) - {stage})
        assert ('round1' in payload) == (stage == 'revise')
        assert ('round2' in payload) == (stage == 'synthesis')
        if stage in {'initial', 'revise'}:
            assert payload['interests'] == ROLE_INTERESTS[payload['role']]
            assert 'proposed_plan' not in instruction
            assert contract['properties']['suggested_minutes']['maximum'] == ceiling
        else:
            plan = contract['$defs']['Plan']
            assert plan['properties']['suggested_minutes']['maximum'] == ceiling
            assert plan['properties']['maximum_minutes']['maximum'] == ceiling
            assert {'maximum_minutes', 'adjustment_conditions'} <= set(plan['required'])
            assert plan['properties']['conditions']['allOf'] == [
                {'contains': {'const': value}} for value in request.constraints]
            if stage == 'baseline':
                assert payload['interests'] == ROLE_INTERESTS

    for round_name in ('round1', 'round2'):
        assert all(0 <= item['suggested_minutes'] <= ceiling
                   for item in report['council'][round_name].values())
    for plan in (report['council']['proposed_plan'], report['single']['advice']['proposed_plan']):
        assert 0 <= plan['suggested_minutes'] <= plan['maximum_minutes'] <= ceiling
        assert set(request.constraints) <= set(plan['conditions'])


@pytest.mark.parametrize('available_minutes, conditional_maximum, expected_status', [
    (None, 0, 'complete'),
    (None, 60, 'complete'),
    (0, 0, 'complete'),
    (0, 60, 'partial'),
])
def test_unanimous_deferral_and_conditional_options_do_not_invent_availability(
        monkeypatch, available_minutes, conditional_maximum, expected_status):
    data = json.loads(Path('examples/demo_request.json').read_text())
    data['available_minutes'] = available_minutes
    request = DeliberationRequest.model_validate(data)
    original = DeliberationFakeModel._generate

    def defer(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        payload = json.loads(messages[-1].content)
        output = json.loads(response.generations[0].message.content)
        if payload['stage'] == 'initial':
            output.update(position='当前暂缓，等待可用时段确认。', suggested_minutes=0,
                          reasons=['本次没有可用于直接排期的已确认时段。'],
                          concerns=[], acceptable_conditions=['确认可用时段后再讨论。'],
                          unknowns=['未来可用时段。'])
        elif payload['stage'] == 'revise':
            output.update(revised_position='仍暂缓；同伴没有提供新的可用时段。',
                          suggested_minutes=0, concessions=[], remaining_objections=[],
                          change_explanation='维持原判断，没有新增让步。')
        elif payload['stage'] == 'synthesis':
            output['proposed_plan'].update(
                text='当前默认暂缓；条件方案需另行确认时段。', suggested_minutes=0,
                maximum_minutes=conditional_maximum,
                adjustment_conditions=['用户确认可用时段后，最多尝试60分钟。']
                if conditional_maximum else [])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(output)))])

    monkeypatch.setattr(DeliberationFakeModel, '_generate', defer)
    result = run_council(request)
    assert result.status == expected_status
    assert result.usage.model_calls == 9
    assert all(position.suggested_minutes == 0 for position in result.round1.values())
    assert all(position.suggested_minutes == 0 and position.concessions == []
               for position in result.round2.values())
    if expected_status == 'complete':
        assert result.proposed_plan.suggested_minutes == 0
        assert result.proposed_plan.maximum_minutes == conditional_maximum
        assert result.unresolved_conflicts == []
    else:
        assert result.proposed_plan is None
        assert result.usage.calls[-1].validation_errors[0].code == 'investment_exceeded'
