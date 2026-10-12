"""Live code paths with real LangChain/OpenAI serialization and no sockets or bills."""
import json
from pathlib import Path

import httpx
import pytest
from langchain_openai.chat_models.base import BaseChatOpenAI

from timex.cli import main, comparison_markdown
from timex.comparison import run_comparison
from timex.config import Limits, ModelSettings
from timex.graph import run_council, run_single
from timex.models import DeliberationRequest
from timex.providers import DeliberationFakeModel, initial, revise, synthesize, baseline, make_model


@pytest.fixture
def request_data():
    return DeliberationRequest.model_validate_json(Path('examples/demo_request.json').read_text())


@pytest.fixture
def settings():
    # Deliberately fictional test prices, never defaults or claimed provider prices.
    return ModelSettings(provider='openai', model_name='test-model', base_url='https://example.invalid/v1',
                         input_usd_per_million=0.1, output_usd_per_million=0.2)


def transport_model(settings, handler=None):
    payloads = []

    def respond(request):
        wire = json.loads(request.content)
        payloads.append(wire)
        payload = json.loads(wire['messages'][-1]['content'])
        output = {'initial': initial, 'revise': revise, 'synthesis': synthesize, 'baseline': baseline}[payload['stage']](payload)
        response = {'id': 'in-memory', 'object': 'chat.completion', 'created': 0, 'model': settings.model_name,
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(output)},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}
        if handler:
            return handler(request, response, len(payloads))
        return httpx.Response(200, json=response)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = BaseChatOpenAI(model=settings.model_name, api_key='test-not-a-secret', base_url=settings.base_url,
                          http_client=client, http_socket_options=(), max_tokens=1600, timeout=1, max_retries=0,
                          use_responses_api=False, stream_usage=False, cache=False,
                          model_kwargs={'response_format': {'type': 'json_object'}})
    return model, payloads


def test_live_comparison_same_facts_schema_wire_limits_and_metering(request_data, settings):
    model, payloads = transport_model(settings)
    report = run_comparison(request_data, model=model, settings=settings, live=True)
    assert report['status'] == 'complete'
    assert report['mode'] == 'live'
    assert len(payloads) == report['total_usage']['model_calls'] == 10
    assert report['total_usage']['input_tokens'] == 1000
    assert report['total_usage']['output_tokens'] == 500
    assert report['total_usage']['estimated_usd'] == pytest.approx(0.0002)
    facts = request_data.model_dump(mode='json')
    for wire in payloads:
        assert wire['model'] == settings.model_name
        assert wire['max_tokens'] == 1600
        assert 'max_completion_tokens' not in wire
        assert wire['response_format'] == {'type': 'json_object'}
        assert 'properties' in wire['messages'][0]['content']
        assert json.loads(wire['messages'][-1]['content'])['request'] == facts
    single_input = json.loads(payloads[-1]['messages'][-1]['content'])
    assert single_input['stage'] == 'baseline'
    assert 'round1' not in single_input and 'round2' not in single_input
    assert len(report['council']['usage']['calls']) == 9
    assert report['single']['usage']['elapsed_seconds'] > 0
    rendered = comparison_markdown(report)
    assert '模式：live' in rendered and '待评分' in rendered
    assert '本地 fake' not in rendered


def test_global_format_retry_and_cost_includes_invalid_output(request_data, settings):
    def broken_once(request, response, attempt):
        if attempt == 1:
            response['choices'][0]['message']['content'] = 'private invalid content'
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, broken_once)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'complete'
    assert result.usage.model_calls == len(payloads) == 10
    assert result.usage.input_tokens == 1000
    assert result.usage.calls[0].status == 'contract_or_limit'
    assert 'private invalid' not in result.model_dump_json()
    assert '上次输出' in payloads[1]['messages'][0]['content']


def test_second_invalid_response_stops_with_partial(request_data, settings):
    def broken(request, response, attempt):
        if attempt in (2, 4):
            response['choices'][0]['message']['content'] = '{}'
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, broken)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(payloads) == 4
    assert set(result.round1) == {'career', 'curiosity'}
    assert result.usage.input_tokens == 400


@pytest.mark.parametrize('status,expected_calls', [(429, 10), (500, 10), (401, 1), (400, 1)])
def test_http_retry_is_bounded_and_sdk_retries_disabled(request_data, settings, status, expected_calls):
    def error_once(request, response, attempt):
        if attempt == 1:
            return httpx.Response(status, json={'error': {'message': 'private body', 'type': 'test'}})
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, error_once)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert len(payloads) == expected_calls
    assert result.usage.model_calls == expected_calls
    # Unknown billing on a failed HTTP attempt must not become a fake zero total.
    assert result.usage.input_tokens is None and result.usage.estimated_usd is None
    assert result.usage.budget_estimated_usd > 0
    assert 'private body' not in result.model_dump_json()


def test_http_timeout_is_not_retried_and_comparison_stops(request_data, settings):
    def timeout(request, response, attempt):
        raise httpx.ReadTimeout('private timeout', request=request)
    model, payloads = transport_model(settings, timeout)
    report = run_comparison(request_data, model=model, settings=settings, live=True)
    assert report['status'] == 'partial' and report['single'] is None
    assert report['council']['usage']['calls'][0]['status'] == 'timeout'
    assert len(payloads) == 1
    assert 'private timeout' not in json.dumps(report)


@pytest.mark.parametrize('limits', [Limits(max_input_tokens=1), Limits(max_total_tokens=1),
                                    Limits(max_estimated_usd_per_run=0.00000001)])
def test_preflight_blocks_before_http(request_data, settings, limits):
    model, payloads = transport_model(settings)
    result = run_council(request_data, model=model, settings=settings, limits=limits, live=True)
    assert result.status == 'partial' and not payloads
    assert result.usage.model_calls == 0


def test_token_limit_and_partial_single(request_data, settings):
    def truncated(request, response, attempt):
        response['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, truncated)
    result = run_single(request_data, model=model, settings=settings, live=True)
    assert result['status'] == 'partial' and result['advice'] is None
    assert len(payloads) == 1
    assert result['usage']['output_tokens'] == 50


def test_unknown_usage_preserves_null_and_reserves_budget(request_data, settings):
    def unknown(request, response, attempt):
        del response['usage']
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, unknown)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'complete'
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    assert result.usage.estimated_usd is None
    assert result.usage.budget_estimated_usd > 0
    assert len(payloads) == 9


def test_schema_and_semantic_errors_share_retry_budget(request_data, settings):
    def invalid(request, response, attempt):
        payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
        data = json.loads(response['choices'][0]['message']['content'])
        if payload['stage'] == 'initial':
            data['evidence_refs'] = ['imaginary_fact']
        response['choices'][0]['message']['content'] = json.dumps(data)
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, invalid)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(payloads) == 2
    assert not result.round1


def test_live_opt_in_applies_to_injected_models(request_data, settings):
    with pytest.raises(ValueError, match='live'):
        run_council(request_data, model=DeliberationFakeModel(), settings=settings)
    with pytest.raises(ValueError):
        run_single(request_data, live=True)


@pytest.mark.parametrize('value', ['nan', 'inf', '-1'])
def test_invalid_prices_refused(monkeypatch, value):
    monkeypatch.setenv('MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('MODEL_NAME', 'test')
    monkeypatch.setenv('MODEL_INPUT_USD_PER_MILLION', value)
    monkeypatch.setenv('MODEL_OUTPUT_USD_PER_MILLION', '1')
    with pytest.raises(ValueError):
        ModelSettings.from_env()


def test_missing_prices_or_key_refused(monkeypatch, settings):
    monkeypatch.setenv('MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('MODEL_NAME', 'test')
    monkeypatch.delenv('MODEL_INPUT_USD_PER_MILLION', raising=False)
    with pytest.raises(ValueError, match='prices'):
        ModelSettings.from_env()
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        make_model(settings=settings, live=True)


def test_factory_preserves_compatible_token_parameter_and_disables_retries(monkeypatch, settings):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-secret')
    model = make_model(settings=settings, live=True, limits=Limits(max_output_tokens=123))
    assert model.max_retries == 0 and model.http_socket_options == ()
    assert model.request_timeout == 60
    payload = model._get_request_payload('Return JSON')
    assert payload['max_tokens'] == 123
    assert payload['response_format'] == {'type': 'json_object'}
    assert model.use_responses_api is False


@pytest.mark.parametrize('kwargs', [{'max_output_tokens': float('nan')}, {'max_retries': 2},
                                   {'max_model_calls': 11}, {'max_total_tokens': 1.5},
                                   {'call_timeout_seconds': float('inf')}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_cli_comparison_and_partial_baseline(tmp_path, monkeypatch):
    path = tmp_path / 'comparison.json'
    assert main(['--compare', '--format', 'json', '--output', str(path)]) == 0
    result = json.loads(path.read_text())
    assert result['mode'] == 'fake' and result['total_usage']['model_calls'] == 10
    assert result['total_usage']['estimated_usd'] == 0
    monkeypatch.setenv('MAX_INPUT_TOKENS', '1')
    assert main(['--baseline', '--output', str(tmp_path / 'partial.json')]) == 1


def test_deepseek_settings_pass_thinking_mode(monkeypatch, settings):
    from dataclasses import replace
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-secret')
    model = make_model(settings=replace(settings, thinking='disabled'), live=True)
    assert model._get_request_payload('Return JSON')['extra_body'] == {'thinking': {'type': 'disabled'}}
    with pytest.raises(ValueError):
        replace(settings, thinking='unknown')


def test_retry_cannot_exceed_reduced_call_budget(request_data, settings):
    def broken_first(request, response, attempt):
        if attempt == 1:
            response['choices'][0]['message']['content'] = '{}'
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, broken_first)
    result = run_council(request_data, model=model, settings=settings, live=True, limits=Limits(max_model_calls=9))
    assert len(payloads) == result.usage.model_calls == 9
    assert result.status == 'partial' and len(result.round2) == 4
    assert result.proposed_plan is None


def test_postflight_usage_over_limit_stops(request_data, settings):
    def over_limit(request, response, attempt):
        response['usage'] = {'prompt_tokens': 20000, 'completion_tokens': 50, 'total_tokens': 20050}
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, over_limit)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(payloads) == 1
    assert result.usage.input_tokens == 20000
    assert result.usage.estimated_usd == pytest.approx(0.00201)


def test_connection_error_retries_once(request_data, settings):
    def disconnected(request, response, attempt):
        raise httpx.ConnectError('private connection detail', request=request)
    model, payloads = transport_model(settings, disconnected)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(payloads) == 2
    assert 'private connection detail' not in result.model_dump_json()


@pytest.mark.parametrize('fault', ['peer_quote', 'plan_constraint', 'equal_time_objection'])
def test_semantic_contracts_reject_invalid_live_result(request_data, settings, fault):
    def corrupt(request, response, attempt):
        payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
        data = json.loads(response['choices'][0]['message']['content'])
        if fault == 'peer_quote' and payload['stage'] == 'revise':
            data['responses_to_others'][0]['cited_position'] = 'fabricated peer quote'
        if fault == 'plan_constraint' and payload['stage'] == 'synthesis':
            data['proposed_plan']['conditions'] = []
        if fault == 'equal_time_objection':
            if payload['stage'] == 'revise':
                data['suggested_minutes'] = 60
                data['revised_position'] = '建议投入 60 分钟，但仍有异议。'
                data['remaining_objections'] = ['实际空闲未知，仍不接受该方案。']
            if payload['stage'] == 'synthesis':
                data['unresolved_conflicts'] = []
        response['choices'][0]['message']['content'] = json.dumps(data)
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, corrupt)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial'
    assert result.proposed_plan is None
    assert len(payloads) <= 10


def test_evidence_contract_is_explicit_and_repair_is_actionable(request_data, settings):
    rejected = 'request.time_summary.category_minutes.sleep'
    def repairable(request, response, attempt):
        wire = json.loads(request.content)
        if attempt == 1:
            data = json.loads(response['choices'][0]['message']['content'])
            data['evidence_refs'] = [rejected]
            response['choices'][0]['message']['content'] = json.dumps(data)
        elif attempt == 2:
            system = wire['messages'][0]['content']
            assert 'unknown_evidence_reference' in system
            assert 'evidence_refs' in system and '不带 request.' in system
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, repairable)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'complete' and result.usage.model_calls == 10
    issue = result.usage.calls[0].validation_errors[0]
    assert issue.code == 'unknown_evidence_reference' and issue.path == 'evidence_refs'
    # Rejected values are never echoed into a retry or diagnostic artifact.
    assert rejected not in payloads[1]['messages'][0]['content']
    schema = json.loads(payloads[0]['messages'][0]['content'].split('\n')[-1])
    assert 'time_summary.category_minutes.sleep' in schema['properties']['evidence_refs']['items']['enum']
    assert rejected not in schema['properties']['evidence_refs']['items']['enum']


@pytest.mark.parametrize('fault,code,path', [
    ('json', 'json_invalid', '$'),
    ('missing', 'missing', 'position'),
    ('extra', 'extra_forbidden', '<unknown>'),
    ('wrong_type', 'int_parsing', 'suggested_minutes'),
    ('evidence', 'unknown_evidence_reference', 'evidence_refs'),
])
def test_diagnostics_expose_codes_and_schema_paths_not_values(request_data, settings, fault, code, path):
    private = 'sensitive-rejected-value-DO-NOT-ECHO'
    def malformed(request, response, attempt):
        data = json.loads(response['choices'][0]['message']['content'])
        if fault == 'missing':
            del data['position']
        elif fault == 'extra':
            data[private] = private
        elif fault == 'wrong_type':
            data['suggested_minutes'] = private
        elif fault == 'evidence':
            data['evidence_refs'] = [private]
        response['choices'][0]['message']['content'] = private if fault == 'json' else json.dumps(data)
        return httpx.Response(200, json=response)
    model, payloads = transport_model(settings, malformed)
    result = run_council(request_data, model=model, settings=settings, live=True)
    assert result.status == 'partial' and len(payloads) == 2
    assert result.usage.calls[0].validation_errors[0].model_dump() == {'code': code, 'path': path}
    assert private not in result.model_dump_json()
    assert private not in payloads[1]['messages'][0]['content']
    assert code in payloads[1]['messages'][0]['content']
    from timex.cli import markdown
    assert code in markdown(result) and private not in markdown(result)


def test_schema_locations_redact_untrusted_mapping_keys():
    from timex.models import SingleAdvice
    from timex.validation import safe_issues
    from pydantic import ValidationError
    private = 'sensitive-dictionary-key'
    try:
        SingleAdvice.model_validate({'considerations': {private: 'some value'}})
    except ValidationError as error:
        issues = safe_issues(error, SingleAdvice)
    assert private not in str(issues)
    assert any('<unknown>' in issue.path for issue in issues)
