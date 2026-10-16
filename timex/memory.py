"""Explicit, confirmed preferences stored as short-term events; no model extraction."""
from datetime import datetime, timezone
import json
import os
from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, DeliberationRequest

STREAM = 'confirmed-preferences'
KIND = 'timex.confirmed-preference.v1'


class Preference(Contract):
    text: str = Field(min_length=1, max_length=500)
    maximum_minutes: int = Field(strict=True, ge=0, le=1440)


class MemoryCommand(Contract):
    operation: Literal['confirm_preference', 'correct_preference', 'delete_preference']
    confirmed: bool = Field(strict=True)
    preference: Preference | None = None

    @model_validator(mode='after')
    def check_confirmation(self):
        if not self.confirmed:
            raise ValueError('explicit user confirmation is required')
        if (self.preference is not None) != (self.operation != 'delete_preference'):
            raise ValueError('preference must be supplied only for a write')
        return self


def connection():
    import boto3
    from botocore.config import Config
    # This first demo is single-tenant: the actor is bound by trusted deployment config.
    # It must be replaced by verified identity mapping before a multi-user deployment.
    memory_id = os.environ['TIMEX_MEMORY_ID']
    actor = os.environ['TIMEX_MEMORY_ACTOR_ID']
    if not memory_id or not actor:
        raise ValueError('missing server Memory configuration')
    client = boto3.client('bedrock-agentcore', config=Config(connect_timeout=5, read_timeout=15,
                                                           retries={'total_max_attempts': 1}))
    return client, {'memoryId': memory_id, 'actorId': actor, 'sessionId': STREAM}


def confirmed_events(client, scope):
    events = []
    token = None
    for _ in range(2):
        page = client.list_events(**scope, includePayloads=True, maxResults=100,
                                  **({'nextToken': token} if token else {}))
        for event in page['events']:
            for payload in event.get('payload', []):
                conversation = payload.get('conversational', {})
                if conversation.get('role') != 'USER':
                    continue
                try:
                    data = json.loads(conversation['content']['text'])
                    if data.get('kind') != KIND or data.get('confirmed') is not True:
                        continue
                    preference = Preference.model_validate(data['preference'])
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                events.append((event, preference))
                break
        token = page.get('nextToken')
        if not token:
            return sorted(events, key=lambda item: (item[0]['eventTimestamp'], item[0]['eventId']))
    raise ValueError('Memory history exceeds bounded scan; refusing an incomplete preference read')


def provenance(scope, event=None, preference=None):
    return {'source': 'aws_memory_short_term', 'mode': 'confirmed_events_without_extraction',
            'actor_binding': 'trusted_single_tenant_server_configuration', 'stream': STREAM,
            'memory_id': scope['memoryId'], 'event_id': event['eventId'] if event else None,
            'preference': preference.model_dump() if preference else None}


def execute(command):
    client, scope = connection()
    previous = confirmed_events(client, scope)
    if command.operation == 'confirm_preference' and previous:
        raise ValueError('an existing preference requires explicit correction')
    if command.operation == 'correct_preference' and not previous:
        raise ValueError('there is no confirmed preference to correct')
    event = None
    if command.preference is not None:
        response = client.create_event(**scope, eventTimestamp=datetime.now(timezone.utc), extractionMode='SKIP',
            payload=[{'conversational': {'role': 'USER', 'content': {'text': json.dumps({
                'kind': KIND, 'confirmed': True, 'preference': command.preference.model_dump()}, ensure_ascii=False)}}}])
        event = response['event']
    # Correction replaces old confirmed events; deletion removes all confirmed
    # revisions in this dedicated actor/stream so an older value cannot resurface.
    for old, _ in previous:
        client.delete_event(**scope, eventId=old['eventId'])
    return {'status': 'complete', 'memory': provenance(scope, event, command.preference),
            'deleted_event_count': len(previous), 'paid_model_calls': 0}


def apply_preference(request):
    client, scope = connection()
    events = confirmed_events(client, scope)
    if not events:
        return request, provenance(scope) | {'applied': False, 'precedence': 'no_confirmed_preference'}
    event, preference = events[-1]
    context = provenance(scope, event, preference)
    data = request.model_dump()
    data['preference_max_minutes'] = preference.maximum_minutes
    if request.available_minutes is not None:
        # Preserve the historical fact even when its cap is not active.
        return DeliberationRequest.model_validate(data), context | {
            'applied': False, 'precedence': 'current_request',
            'conflict': request.available_minutes != preference.maximum_minutes,
            'effective_investment_ceiling': request.investment_ceiling,
            'available_minutes': request.available_minutes}
    data['constraints'].append('用户已确认的历史默认投入上限（不是实际可用时间；当前明确要求可覆盖）：' + preference.text)
    value = DeliberationRequest.model_validate(data)
    return value, context | {'applied': True, 'precedence': 'confirmed_memory_default',
                             'effective_investment_ceiling': value.investment_ceiling,
                             'available_minutes': value.available_minutes}
