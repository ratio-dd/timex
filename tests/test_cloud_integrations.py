import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from botocore.exceptions import ClientError
import pytest


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('integrations_cloud', Path('scripts/integrations_cloud.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'STATE', tmp_path / 'state.json')
    return module


def test_lambda_role_propagation_retry_has_a_fixed_bound(cloud, monkeypatch):
    attempts = []
    sleeps = []
    monkeypatch.setattr(cloud.time, 'sleep', sleeps.append)

    def create(**parameters):
        attempts.append(parameters)
        raise ClientError({'Error': {'Code': 'InvalidParameterValueException',
                                     'Message': 'The role defined for the function cannot be assumed by Lambda.'}},
                          'CreateFunction')

    with pytest.raises(ClientError):
        cloud.create_lambda(SimpleNamespace(create_function=create), FunctionName='synthetic')
    assert len(attempts) == 4 and sleeps == [3, 3, 3]


@pytest.mark.parametrize('strategies, execution_role', [([{'semanticMemoryStrategy': {}}], None),
                                                      ([], 'unexpected-model-extraction-role')])
def test_memory_deployment_rejects_background_extraction_before_granting_permissions(cloud, monkeypatch,
                                                                                   strategies, execution_role):
    monkeypatch.setattr(cloud, 'state_for', lambda account: {'memory_id': 'synthetic-memory-1234567890'})
    memory = {'status': 'ACTIVE', 'strategies': strategies, 'memoryExecutionRoleArn': execution_role}

    def client(service):
        assert service == 'bedrock-agentcore-control', 'No IAM changes allowed for an unexpected Memory mode'
        return SimpleNamespace(get_memory=lambda **kwargs: {'memory': memory})

    with pytest.raises(ValueError, match='without automatic extraction'):
        cloud.deploy_memory(SimpleNamespace(client=client), '123456789012')


def test_memory_runtime_permissions_are_scoped_to_one_memory_without_model_access(cloud, monkeypatch):
    arn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/timex_preferences-1234567890'
    monkeypatch.setattr(cloud, 'state_for', lambda account: {'memory_id': 'timex_preferences-1234567890',
                                                           'memory_arn': arn})
    applied = []
    memory = {'status': 'ACTIVE', 'strategies': [], 'eventExpiryDuration': 7}

    def client(service):
        if service == 'bedrock-agentcore-control':
            return SimpleNamespace(get_memory=lambda **kwargs: {'memory': memory})
        assert service == 'iam'
        return SimpleNamespace(put_role_policy=lambda **kwargs: applied.append(kwargs))

    cloud.deploy_memory(SimpleNamespace(client=client), '123456789012')
    document = json.loads(applied[0]['PolicyDocument'])
    assert applied[0]['RoleName'] == 'timex-runtime'
    statement = document['Statement'][0]
    assert statement['Resource'] == arn
    assert statement['Action'] == ['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents',
                                    'bedrock-agentcore:GetEvent', 'bedrock-agentcore:DeleteEvent']
    assert 'bedrock:InvokeModel' not in json.dumps(document)


def test_secret_file_is_parsed_without_shell_execution(cloud, tmp_path):
    path = tmp_path / '.env'
    marker = tmp_path / 'must-not-exist'
    path.write_text(f"OTHER=$(touch {marker})\nexport OPENAI_API_KEY='synthetic-key' # selected\n")
    path.chmod(0o600)
    assert cloud.read_api_key(path) == 'synthetic-key'
    assert not marker.exists()


@pytest.mark.parametrize('contents, mode', [
    ('OPENAI_API_KEY=synthetic', 0o644),
    ('OTHER=unrelated', 0o600),
    ('OPENAI_API_KEY=', 0o600),
    ('OPENAI_API_KEY="unterminated', 0o600),
    ('OPENAI_API_KEY=one\nDEEPSEEK_API_KEY=another', 0o600),
])
def test_secret_file_refuses_public_permissions_missing_or_ambiguous_keys(cloud, tmp_path, contents, mode):
    path = tmp_path / '.env'
    path.write_text(contents)
    path.chmod(mode)
    with pytest.raises(ValueError):
        cloud.read_api_key(path)


@pytest.mark.parametrize('live, provider', [('1', 'fake'), ('0', 'openai')])
def test_secret_preparation_requires_fake_only_runtime_before_reading_key(cloud, monkeypatch, live, provider):
    monkeypatch.setattr(cloud, 'runtime_state', lambda account: {'agentRuntimeId': 'synthetic'})
    control = SimpleNamespace(
        get_agent_runtime=lambda **kwargs: {'agentRuntimeArn': 'synthetic',
            'environmentVariables': {'TIMEX_ALLOW_LIVE': live, 'MODEL_PROVIDER': provider}},
        list_tags_for_resource=lambda **kwargs: {'tags': cloud.TAGS})
    monkeypatch.setattr(cloud, 'read_api_key', lambda path: pytest.fail('Must check fake-only before reading key'))
    with pytest.raises(ValueError, match='fake-only'):
        cloud.upload_secret(SimpleNamespace(client=lambda name: control), '123456789012', Path('.env'))


@pytest.mark.parametrize('existing_owned_secret', [False, True])
def test_secret_upload_never_writes_key_to_evidence_or_runtime_environment(cloud, monkeypatch, tmp_path,
                                                                         capsys, existing_owned_secret):
    account = '123456789012'
    arn = f'arn:aws:secretsmanager:us-east-1:{account}:secret:timex/deepseek-AbCdEf'
    key = 'PRIVATE_CANARY_API_KEY'
    monkeypatch.setattr(cloud, 'runtime_state', lambda account: {'agentRuntimeId': 'synthetic'})
    path = tmp_path / '.env'
    path.write_text('OPENAI_API_KEY=' + key + '\nOTHER_SECRET=MUST_NOT_UPLOAD\n')
    path.chmod(0o600)
    calls = []
    policies = []

    class NotFound(Exception):
        pass

    class Secrets:
        exceptions = SimpleNamespace(ResourceNotFoundException=NotFound)

        def describe_secret(self, **kwargs):
            if existing_owned_secret:
                return {'ARN': arn, 'Tags': [{'Key': k, 'Value': v} for k, v in cloud.TAGS.items()]}
            raise NotFound()

        def create_secret(self, **kwargs):
            assert not existing_owned_secret and 'KmsKeyId' not in kwargs
            calls.append(kwargs)
            return {'ARN': arn, 'VersionId': 'synthetic-version'}

        def put_secret_value(self, **kwargs):
            assert existing_owned_secret and kwargs['SecretId'] == arn
            calls.append(kwargs)
            return {'ARN': arn, 'VersionId': 'synthetic-version'}

        def get_secret_value(self, **kwargs):
            assert kwargs == {'SecretId': arn, 'VersionId': 'synthetic-version'}
            return {'SecretString': calls[0]['SecretString']}

    control = SimpleNamespace(
        get_agent_runtime=lambda **kwargs: {'agentRuntimeArn': 'synthetic',
            'environmentVariables': {'TIMEX_ALLOW_LIVE': '0', 'MODEL_PROVIDER': 'fake'}},
        list_tags_for_resource=lambda **kwargs: {'tags': cloud.TAGS})
    iam = SimpleNamespace(get_role=lambda **kwargs: {'Role': {'Tags': [
        {'Key': k, 'Value': v} for k, v in cloud.TAGS.items()]}},
        put_role_policy=lambda **kwargs: policies.append(kwargs))
    clients = {'bedrock-agentcore-control': control, 'iam': iam, 'secretsmanager': Secrets()}
    cloud.upload_secret(SimpleNamespace(client=clients.__getitem__), account, path)
    assert len(calls) == 1 and json.loads(calls[0]['SecretString']) == {'OPENAI_API_KEY': key}
    document = json.loads(policies[0]['PolicyDocument'])
    assert document['Statement'] == [{'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': arn}]
    evidence = capsys.readouterr().out + ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    assert key not in evidence and 'MUST_NOT_UPLOAD' not in evidence
    assert 'TIMEX_ALLOW_LIVE' not in json.dumps(calls)
