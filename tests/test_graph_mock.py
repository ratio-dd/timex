import json
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from timex.graph import Limits, run_council, run_single
from timex.models import DeliberationRequest, ROLES
from timex.providers import DeliberationFakeModel


def request():
    return DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())


def test_nine_calls_independent_first_round_and_peer_revisions(monkeypatch):
    payloads = []
    original = DeliberationFakeModel._generate
    def record(self, messages, **kwargs):
        payloads.append(json.loads(messages[-1].content))
        return original(self, messages, **kwargs)
    monkeypatch.setattr(DeliberationFakeModel, '_generate', record)
    result = run_council(request())
    assert result.status == 'complete'
    assert result.usage.model_calls == 9
    assert set(result.round1) == set(result.round2) == set(ROLES)
    assert all('round1' not in item and 'round2' not in item for item in payloads[:4])
    assert all(item['request'] == payloads[0]['request'] for item in payloads)
    assert all(item['round1'] == payloads[4]['round1'] for item in payloads[4:8])
    assert any(result.round1[role].suggested_minutes != result.round2[role].suggested_minutes for role in ROLES)
    for role, revision in result.round2.items():
        assert {response.role for response in revision.responses_to_others} == set(ROLES) - {role}
        assert all(response.cited_position == result.round1[response.role].position for response in revision.responses_to_others)
    assert result.unresolved_conflicts
    assert result.requires_user_decision


def test_peer_change_changes_revision(monkeypatch):
    baseline = run_council(request())
    original = DeliberationFakeModel._generate
    def change_life(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        payload = json.loads(messages[-1].content)
        if payload['stage'] == 'initial' and payload['role'] == 'life':
            content = json.loads(response.generations[0].message.content)
            content.update(suggested_minutes=15, position='建议新增投入 15 分钟。')
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(content)))])
        return response
    monkeypatch.setattr(DeliberationFakeModel, '_generate', change_life)
    changed = run_council(request())
    assert changed.status == 'complete'
    assert baseline.round2['career'].suggested_minutes == 60
    assert changed.round2['career'].suggested_minutes == 15


def test_role_not_fixed_support():
    normal = run_council(request())
    noncareer = run_council(request().model_copy(update={'question': '无明显职业收益但有趣的非功利探索是否值得？'}))
    assert normal.round1['career'].suggested_minutes > 0
    assert noncareer.round1['career'].suggested_minutes == 0
    assert noncareer.round1['curiosity'].suggested_minutes > 0


def test_budget_exhaustion_returns_partial_without_extra_calls():
    result = run_council(request(), limits=Limits(max_model_calls=5))
    assert result.status == 'partial'
    assert result.usage.model_calls == 5
    assert len(result.round1) == 4 and len(result.round2) == 1
    assert result.proposed_plan is None
    assert result.errors


def test_timeout_stops_no_followup(monkeypatch):
    original = DeliberationFakeModel._generate
    def slow(self, messages, **kwargs):
        time.sleep(0.1)
        return original(self, messages, **kwargs)
    monkeypatch.setattr(DeliberationFakeModel, '_generate', slow)
    result = run_council(request(), limits=Limits(call_timeout_seconds=0.01))
    assert result.status == 'partial'
    assert result.usage.model_calls == 1


def test_malformed_output_not_retried(monkeypatch):
    def broken(self, messages, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='private invalid output'))])
    monkeypatch.setattr(DeliberationFakeModel, '_generate', broken)
    result = run_council(request())
    assert result.status == 'partial' and result.usage.model_calls == 1
    assert 'private' not in str(result.errors)


def test_size_limits_and_total_timeout():
    result = run_council(request(), limits=Limits(max_input_characters=1))
    assert result.status == 'partial' and result.usage.model_calls == 0
    result = run_council(request(), limits=Limits(max_output_characters=1))
    assert result.status == 'partial' and result.usage.model_calls == 1
    result = run_council(request(), limits=Limits(total_timeout_seconds=0.000000001))
    assert result.status == 'partial' and result.usage.model_calls == 0


def test_live_provider_rejected():
    with pytest.raises(ValueError, match='fake'):
        run_council(request(), provider='bedrock')
    with pytest.raises(ValueError):
        Limits(max_model_calls=11)


def test_single_baseline():
    result = run_single(request())
    assert result['usage']['model_calls'] == 1
    assert result['perspective'] == 'balanced'
    assert set(result['advice']['considerations']) == set(ROLES)


def test_adequate_recorded_sleep_changes_health_and_allows_agreement():
    data = request().model_dump(mode='json')
    facts = data['time_summary']
    facts['category_minutes']['sleep'] += 840
    facts['category_minutes']['untracked'] -= 840
    facts['tracked_minutes'] += 840
    facts['untracked_minutes'] -= 840
    facts['coverage'] = facts['tracked_minutes'] / 10080
    result = run_council(DeliberationRequest.model_validate(data))
    assert result.status == 'complete'
    assert result.round1['health'].suggested_minutes == 120
    assert {item.suggested_minutes for item in result.round2.values()} == {60}
    assert result.unresolved_conflicts == []
