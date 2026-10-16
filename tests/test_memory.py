import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from timex import memory, runtime
from timex.models import DeliberationRequest


@pytest.fixture
def request_data():
    return json.loads(Path('examples/demo_request.json').read_text())


@pytest.fixture
def store(monkeypatch):
    class Store:
        def __init__(self):
            self.events = []
            self.sequence = 0

        def list_events(self, **kwargs):
            assert kwargs['actorId'] == 'server-bound-synthetic-actor'
            assert kwargs['sessionId'] == memory.STREAM
            return {'events': copy.deepcopy(list(reversed(self.events)))}

        def create_event(self, **kwargs):
            assert kwargs['extractionMode'] == 'SKIP'
            assert kwargs['payload'][0]['conversational']['role'] == 'USER'
            self.sequence += 1
            event = {'eventId': f'1000#{self.sequence:08x}', 'eventTimestamp': kwargs['eventTimestamp'],
                     'payload': kwargs['payload']}
            self.events.append(event)
            return {'event': event}

        def delete_event(self, **kwargs):
            assert kwargs['actorId'] == 'server-bound-synthetic-actor'
            self.events = [item for item in self.events if item['eventId'] != kwargs['eventId']]
            return {}

    value = Store()
    monkeypatch.setattr(memory, 'connection', lambda: (value, {
        'memoryId': 'synthetic-memory-1234567890', 'actorId': 'server-bound-synthetic-actor', 'sessionId': memory.STREAM}))
    return value


def confirm(minutes=30):
    return {'operation': 'confirm_preference', 'confirmed': True,
            'preference': {'text': '合成确认偏好：TimeX 默认最多投入30分钟', 'maximum_minutes': minutes}}


@pytest.mark.parametrize('change', [{'confirmed': False}, {'confirmed': 1}, {'confirmed': 'true'},
                                   {'actorId': 'someone-else'}, {'memoryId': 'other'},
                                   {'preference': {'text': 'unbounded', 'maximum_minutes': -1}}])
def test_unconfirmed_or_spoofed_writes_are_rejected_before_aws(monkeypatch, change):
    monkeypatch.setattr(memory, 'connection', lambda: pytest.fail('Invalid command must not touch AWS'))
    body, status = runtime.handle_invocation(confirm() | change)
    assert status == 400 and body['errors'] == ['invalid_memory_command']


def test_confirm_read_correct_delete_across_new_requests(store, request_data):
    before, _ = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert before.available_minutes is None
    saved, status = runtime.handle_invocation(confirm())
    assert status == 200 and saved['memory']['event_id']
    body, status = runtime.handle_invocation({'request': request_data, 'use_memory': True})
    assert status == 200 and body['usage']['provider'] == 'fake'
    assert body['context_sources']['memory']['effective_investment_ceiling'] == 30
    assert body['context_sources']['memory']['available_minutes'] is None
    assert all(p['suggested_minutes'] <= 30 for p in body['round1'].values())
    corrected, status = runtime.handle_invocation(confirm(45) | {'operation': 'correct_preference'})
    assert status == 200 and corrected['deleted_event_count'] == 1
    assert len(store.events) == 1
    value, source = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert value.available_minutes is None and value.preference_max_minutes == 45
    assert source['event_id'] == corrected['memory']['event_id']
    deleted, status = runtime.handle_invocation({'operation': 'delete_preference', 'confirmed': True})
    assert status == 200 and deleted['deleted_event_count'] == 1
    assert store.events == []
    value, source = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert value.available_minutes is None and source['preference'] is None
    assert request_data.get('available_minutes') is None


@pytest.mark.parametrize('current', [0, 10, 60])
def test_current_structured_limit_overrides_remembered_default(store, request_data, current):
    runtime.handle_invocation(confirm())
    request_data['available_minutes'] = current
    request = DeliberationRequest.model_validate(request_data)
    value, source = memory.apply_preference(request)
    assert value.available_minutes == current
    assert value.preference_max_minutes == 30 and value.investment_ceiling == current
    assert value.constraints == request.constraints
    assert source['precedence'] == 'current_request' and source['applied'] is False
    assert source['conflict'] is True
    assert source['available_minutes'] == current and source['effective_investment_ceiling'] == current
    from timex.calling import messages_for
    from timex.models import InitialPosition
    instruction = messages_for({'stage':'initial','request':value.model_dump(mode='json')}, InitialPosition)[0].content
    assert f'当前明确上限{current}分钟覆盖它' in instruction
    assert '历史偏好保留为来源，本轮不生效' in instruction


def test_assistant_suggestions_and_unconfirmed_events_never_become_preferences(store, request_data):
    for role, confirmed in [('ASSISTANT', True), ('USER', False)]:
        store.events.append({'eventId': '1000#abcd', 'eventTimestamp': datetime.now(timezone.utc), 'payload': [
            {'conversational': {'role': role, 'content': {'text': json.dumps({
                'kind': memory.KIND, 'confirmed': confirmed, 'preference': confirm()['preference']})}}}]})
    value, source = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert value.available_minutes is None and source['preference'] is None


def test_memory_failure_stops_deliberation_without_injecting_a_fallback(monkeypatch, request_data):
    def fail():
        raise RuntimeError('PRIVATE_AWS_RESPONSE')
    monkeypatch.setattr(memory, 'connection', fail)
    monkeypatch.setattr(runtime, 'run_council', lambda *a, **k: pytest.fail('Memory fetch failure must stop'))
    body, status = runtime.handle_invocation({'request': request_data, 'use_memory': True})
    assert status == 503 and body == {'status': 'error', 'errors': ['memory_unavailable']}


def test_memory_actor_comes_only_from_trusted_environment(monkeypatch):
    import boto3
    sentinel = object()
    monkeypatch.setenv('TIMEX_MEMORY_ID', 'synthetic-memory-1234567890')
    monkeypatch.setenv('TIMEX_MEMORY_ACTOR_ID', 'server-bound-synthetic-actor')
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: sentinel)
    client, scope = memory.connection()
    assert client is sentinel
    assert scope == {'memoryId': 'synthetic-memory-1234567890', 'actorId': 'server-bound-synthetic-actor',
                     'sessionId': memory.STREAM}


def test_incomplete_paginated_history_is_not_used_as_current_preference(store):
    store.list_events = lambda **kwargs: {'events': [], 'nextToken': 'more'}
    with pytest.raises(ValueError, match='bounded'):
        memory.confirmed_events(store, {})


@pytest.mark.parametrize('minutes', [0, 30])
def test_memory_cap_preserves_confirmed_time_facts_and_does_not_invent_availability(store, request_data, minutes):
    request_data['constraints'].append('已确认有90分钟自主时段，不必用满。')
    runtime.handle_invocation(confirm(minutes))
    value, source = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert value.available_minutes is None and value.preference_max_minutes == minutes
    assert value.investment_ceiling == minutes
    assert '已确认有90分钟自主时段，不必用满。' in value.constraints
    assert source['available_minutes'] is None and source['effective_investment_ceiling'] == minutes
    body, status = runtime.handle_invocation({'request': request_data, 'use_memory': True})
    assert status == 200 and body['status'] == 'complete'
    assert body['proposed_plan']['maximum_minutes'] <= minutes
    from timex.calling import messages_for
    from timex.models import InitialPosition
    messages = messages_for({'stage':'initial','request':value.model_dump(mode='json')}, InitialPosition)
    assert f'本轮有效投入总上限为{minutes}分钟' in messages[0].content
    assert 'requested_minutes是询问的候选总额，不构成覆盖偏好' in messages[0].content


def test_http_cannot_inject_server_memory_cap(request_data):
    body, status = runtime.handle_invocation({'request': request_data | {'preference_max_minutes': 500}})
    assert status == 400 and body['errors'] == ['invalid_request']
