"""Semantic regression cases and real SDK strict-tool wire tests, with no sockets."""
import json
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai.chat_models.base import BaseChatOpenAI

from timex.calling import BoundedCaller, LimitError, messages_for, output_shape
from timex.comparison import run_comparison
from timex.config import Limits, ModelSettings
from timex.graph import run_council
from timex.models import DeliberationRequest, InitialPosition
from timex.providers import DeliberationFakeModel, initial, revise, synthesize, baseline, make_model


def request():
    return DeliberationRequest.model_validate_json(Path('examples/agentcore_live_request.json').read_text())


@pytest.mark.parametrize('change_minutes', [False, True])
def test_old_condition_cannot_be_called_a_new_concession(monkeypatch, change_minutes):
    original = DeliberationFakeModel._generate

    def repeat(self, messages, **kwargs):
        response = original(self, messages, **kwargs)
        payload = json.loads(messages[-1].content)
        if payload['stage'] == 'revise':
            data = json.loads(response.generations[0].message.content)
            own = payload['round1'][payload['role']]
            data['suggested_minutes'] = max(0, own['suggested_minutes'] - 1) if change_minutes else own['suggested_minutes']
            data['concessions'] = [own['acceptable_conditions'][0]]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(data)))])
        return response

    monkeypatch.setattr(DeliberationFakeModel, '_generate', repeat)
    result = run_council(request())
    assert result.status == 'partial'
    assert result.usage.calls[-1].validation_errors[0].code == 'concession_change_required'


def strict_transport(damage=None):
    settings = ModelSettings(provider='openai', model_name='test-model', base_url='https://api.deepseek.com/beta',
                             output_mode='deepseek_strict_tools', input_usd_per_million=0.3, output_usd_per_million=1.2)
    calls = []

    def respond(request):
        wire = json.loads(request.content)
        calls.append(wire)
        assert request.url.path == '/beta/chat/completions'
        payload = json.loads(wire['messages'][-1]['content'])
        output = {'initial': initial, 'revise': revise, 'synthesis': synthesize, 'baseline': baseline}[payload['stage']](payload)
        function = {'name': 'submit_' + payload['stage'], 'arguments': json.dumps(output)}
        tools = [{'id': 'in-memory-call', 'type': 'function', 'function': function}]
        if damage == 'json':
            function['arguments'] = 'PRIVATE_INVALID_JSON'
        elif damage == 'extra':
            function['arguments'] = json.dumps(output | {'PRIVATE_FIELD': 'PRIVATE_VALUE'})
        elif damage == 'wrong-channel':
            function['name'] = 'execute_unapproved_tool'
        elif damage == 'multiple':
            tools *= 2
        return httpx.Response(200, json={'id': 'mock', 'object': 'chat.completion', 'created': 0,
            'model': 'test-model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None,
                'tool_calls': tools}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}})

    model = BaseChatOpenAI(model='test-model', api_key='test-not-a-secret', base_url=settings.base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)), http_socket_options=(),
        max_retries=0, max_tokens=1600, use_responses_api=False, stream_usage=False)
    return settings, model, calls


def assert_closed_schema(schema):
    assert not set(schema) & {'$ref', '$defs', 'minItems', 'maxItems', 'minLength', 'maxLength', 'allOf', 'contains'}
    if schema.get('type') == 'object':
        assert schema['additionalProperties'] is False
        assert set(schema['required']) == set(schema['properties'])
        for child in schema['properties'].values():
            assert_closed_schema(child)
    if 'items' in schema:
        assert_closed_schema(schema['items'])


def test_strict_tools_use_real_sdk_closed_contracts_and_meter_all_stages():
    settings, model, calls = strict_transport()
    result = run_comparison(request(), model=model, settings=settings, live=True)
    assert result['status'] == 'complete' and len(calls) == result['total_usage']['model_calls'] == 10
    assert result['total_usage']['input_tokens'] == 1000
    for wire in calls:
        assert 'response_format' not in wire
        assert len(wire['messages']) == 2
        tool = wire['tools'][0]['function']
        assert tool['strict'] is True
        assert wire['tool_choice']['function']['name'] == tool['name']
        assert_closed_schema(tool['parameters'])
        instruction = wire['messages'][0]['content']
        assert '仅通过指定输出函数的参数提交结果' in instruction
        assert '只输出符合下列 schema 的 JSON 对象' not in instruction
        assert '"$defs"' not in instruction
        if tool['name'] == 'submit_revise':
            peer = tool['parameters']['properties']['responses_to_others']['items']
            assert len(peer['properties']['cited_position']['enum']) == 3


@pytest.mark.parametrize('damage, code', [('json', 'json_invalid'), ('extra', 'extra_forbidden'),
                                       ('wrong-channel', 'output_channel_invalid'), ('multiple', 'output_channel_invalid')])
def test_strict_output_still_rejects_invalid_results_and_keeps_one_retry(damage, code):
    settings, model, calls = strict_transport(damage)
    result = run_council(request(), model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(calls) == result.usage.model_calls == 2
    assert result.usage.calls[-1].validation_errors[0].code == code
    assert result.usage.input_tokens == 200 and 'PRIVATE' not in result.model_dump_json()


def test_strict_schema_is_included_in_the_preflight_token_reservation():
    settings, model, calls = strict_transport()
    payload = {'stage': 'initial', 'role': 'career', 'interests': 'career', 'request': request().model_dump(mode='json')}
    messages = messages_for(payload, InitialPosition)
    message_bytes = sum(len(m.content.encode()) + 32 for m in messages)
    caller = BoundedCaller(model, Limits(max_input_tokens=message_bytes), settings)
    with pytest.raises(LimitError, match='input token'):
        caller.invoke(payload, InitialPosition)
    assert calls == [] and caller.calls == 0


def test_strict_factory_requires_beta_and_omits_json_object_mode(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-secret')
    settings, _, _ = strict_transport()
    model = make_model(settings=settings, live=True)
    assert 'response_format' not in model._get_request_payload('Return a tool result')
    with pytest.raises(ValueError, match='beta endpoint'):
        ModelSettings(provider='openai', base_url='https://api.deepseek.com', output_mode='deepseek_strict_tools')


@pytest.mark.parametrize('content, shape', [(None, 'not_text'), ('PRIVATE_INVALID', 'invalid_json'),
    ('"PRIVATE_VALUE"', 'json_string'), ('["PRIVATE_VALUE"]', 'json_non_object'),
    ('{"PRIVATE_KEY":"PRIVATE_VALUE"}', 'json_object'),
    ('{"type":"object","properties":{"PRIVATE_KEY":{}}}', 'schema_wrapper')])
def test_output_diagnostics_retain_no_values_or_unknown_keys(content, shape):
    assert output_shape(content) == shape and 'PRIVATE' not in output_shape(content)
