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
    assert body['context_sources']['memory']['effective_available_minutes'] == 30
    assert all(p['suggested_minutes'] <= 30 for p in body['round1'].values())
    corrected, status = runtime.handle_invocation(confirm(45) | {'operation': 'correct_preference'})
    assert status == 200 and corrected['deleted_event_count'] == 1
    assert len(store.events) == 1
    value, source = memory.apply_preference(DeliberationRequest.model_validate(request_data))
    assert value.available_minutes == 45 and source['event_id'] == corrected['memory']['event_id']
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
    assert value.constraints == request.constraints
    assert source['precedence'] == 'current_request' and source['applied'] is False
    assert source['conflict'] is True


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
