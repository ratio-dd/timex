"""Exercise the actual AgentCore HTTP adapter without sockets, AWS, or paid models."""
import asyncio
import json
from pathlib import Path

import boto3
import httpx
import pytest

from timex import runtime


@pytest.fixture
def invocation():
    return {'request': json.loads(Path('examples/demo_request.json').read_text())}


@pytest.fixture(autouse=True)
def server_environment(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setenv('TIMEX_DEEPSEEK_SECRET_ARN', '')
    monkeypatch.setenv('TIMEX_ALLOW_LIVE', '0')


def request(app, method, path, **kwargs):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://runtime.test', trust_env=False) as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(send())


def test_sdk_health_and_default_fake_without_credentials(monkeypatch, invocation, tmp_path):
    def forbidden():
        raise AssertionError('A fake invocation must not fetch credentials')
    monkeypatch.setattr(runtime, '_load_api_key', forbidden)
    monkeypatch.setenv('MODEL_PROVIDER', 'openai')
    # A local .env must not silently turn an HTTP dry run into a paid request.
    (tmp_path / '.env').write_text('OPENAI_API_KEY=DO_NOT_LOAD\n')
    monkeypatch.chdir(tmp_path)
    app = runtime.create_app()
    assert request(app, 'GET', '/ping').json()['status'] == 'Healthy'
    response = request(app, 'POST', '/invocations', json=invocation)
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'complete'
    assert body['usage']['provider'] == 'fake'
    assert body['usage']['model_calls'] == 9
    assert body['usage']['estimated_usd'] == 0
    assert runtime.os.environ['OPENAI_API_KEY'] == ''


@pytest.mark.parametrize('change', [
    {'live': 'true'}, {'live': 1}, {'output': '/tmp/PRIVATE_PATH'},
    {'limits': {'max_model_calls': 1000}}, {'OPENAI_API_KEY': 'PRIVATE_KEY'},
    {'request': {'question': 'PRIVATE_MISSING_FACTS'}},
])
def test_invalid_http_input_is_rejected_without_echo_or_execution(monkeypatch, invocation, change):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid input must not execute a council')
    monkeypatch.setattr(runtime, 'run_council', forbidden)
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation | change)
    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'errors': ['invalid_request']}
    assert 'PRIVATE' not in response.text


def test_explicit_live_request_also_requires_server_opt_in(monkeypatch, invocation):
    monkeypatch.setattr(runtime, '_load_api_key', lambda: pytest.fail('Credential lookup must not run'))
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation | {'live': True})
    assert response.status_code == 403
    assert response.json()['errors'] == ['live_disabled']


def test_server_configuration_is_validated_before_secret_lookup(monkeypatch, invocation):
    monkeypatch.setenv('TIMEX_ALLOW_LIVE', '1')
    monkeypatch.setenv('MODEL_PROVIDER', 'fake')
    monkeypatch.setattr(runtime, '_load_api_key', lambda: pytest.fail('Credential lookup must not run'))
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation | {'live': True})
    assert response.status_code == 503
    assert response.json()['errors'] == ['invalid_server_configuration']


def test_partial_experiment_is_preserved_as_partial(monkeypatch, invocation):
    monkeypatch.setenv('MAX_MODEL_CALLS', '1')
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation)
    assert response.status_code == 200
    assert response.json()['status'] == 'partial'
    assert response.json()['usage']['model_calls'] == 1
    assert response.json()['proposed_plan'] is None


def test_secret_lookup_uses_deployment_arn_and_caches_key(monkeypatch):
    secret_arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:timex/deepseek-example'
    monkeypatch.setenv('TIMEX_DEEPSEEK_SECRET_ARN', secret_arn)
    lookups = []

    class Secrets:
        def get_secret_value(self, **kwargs):
            lookups.append(kwargs)
            return {'SecretString': json.dumps({'OPENAI_API_KEY': 'synthetic-test-key'})}

    monkeypatch.setattr(boto3, 'client', lambda name: Secrets() if name == 'secretsmanager' else None)
    runtime._load_api_key()
    runtime._load_api_key()
    assert lookups == [{'SecretId': secret_arn}]
    assert runtime.os.environ['OPENAI_API_KEY'] == 'synthetic-test-key'


def test_credential_errors_do_not_leak_into_http_or_logs(monkeypatch, invocation, caplog):
    monkeypatch.setenv('TIMEX_ALLOW_LIVE', '1')
    monkeypatch.setenv('MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('MODEL_NAME', 'test-model')
    monkeypatch.setenv('MODEL_INPUT_USD_PER_MILLION', '0.3')
    monkeypatch.setenv('MODEL_OUTPUT_USD_PER_MILLION', '1.2')

    def broken():
        raise RuntimeError('PRIVATE_KEY_AND_AWS_RESPONSE')

    monkeypatch.setattr(runtime, '_load_api_key', broken)
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation | {'live': True})
    assert response.status_code == 503
    assert response.json()['errors'] == ['credential_unavailable']
    assert 'PRIVATE' not in response.text + caplog.text


def test_live_path_uses_server_configuration_and_loads_credentials_first(monkeypatch, invocation):
    result = runtime.run_council(
        runtime.RuntimeInvocation.model_validate(invocation).request,
        settings=runtime.ModelSettings(), limits=runtime.Limits())
    monkeypatch.setenv('TIMEX_ALLOW_LIVE', '1')
    monkeypatch.setenv('MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('MODEL_NAME', 'synthetic-server-model')
    monkeypatch.setenv('MODEL_INPUT_USD_PER_MILLION', '0.3')
    monkeypatch.setenv('MODEL_OUTPUT_USD_PER_MILLION', '1.2')
    monkeypatch.setenv('MAX_ESTIMATED_USD_PER_RUN', '0.05')
    steps = []
    monkeypatch.setattr(runtime, '_load_api_key', lambda: steps.append('credential'))

    def council(value, *, settings, limits, live):
        assert steps == ['credential']
        assert value.question == invocation['request']['question']
        assert settings.provider == 'openai'
        assert settings.model_name == 'synthetic-server-model'
        assert limits.max_estimated_usd_per_run == 0.05
        assert live is True
        steps.append('execute')
        return result

    monkeypatch.setattr(runtime, 'run_council', council)
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation | {'live': True})
    assert response.status_code == 200
    assert steps == ['credential', 'execute']


def test_unexpected_execution_error_is_sanitized(monkeypatch, invocation, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('PRIVATE_REQUEST_AND_KEY')

    monkeypatch.setattr(runtime, 'run_council', broken)
    response = request(runtime.create_app(), 'POST', '/invocations', json=invocation)
    assert response.status_code == 500
    assert response.json()['errors'] == ['execution_failed']
    assert 'PRIVATE' not in response.text + caplog.text


def test_concurrent_invocations_have_independent_requests(invocation):
    first = invocation
    second = json.loads(json.dumps(invocation))
    second['request'].update(question='另一个合成议题', available_minutes=0)
    app = runtime.create_app()

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://runtime.test', trust_env=False) as client:
            return await asyncio.gather(*(client.post('/invocations', json=value) for value in (first, second)))

    responses = asyncio.run(send())
    assert all(response.status_code == 200 for response in responses)
    assert [response.json()['question'] for response in responses] == [
        first['request']['question'], second['request']['question']]
    assert responses[1].json()['proposed_plan']['maximum_minutes'] == 0
