"""AWS deployment guards without any credentials or service calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from timex.runtime_deploy import APPLICATION_FILES

spec = importlib.util.spec_from_file_location('runtime_cloud', Path('scripts/runtime_cloud.py'))
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


def test_existing_unowned_resources_are_never_adopted():
    for tags in ({}, {'Project': 'Other'}, [{'Key': 'Project', 'Value': 'TimeX'}]):
        with pytest.raises(ValueError, match='not owned'):
            cloud.require_owned(tags)
    cloud.require_owned(cloud.TAGS)


def test_deploy_rejects_root_before_upload_or_runtime_creation(monkeypatch):
    monkeypatch.setattr(cloud, 'load_state', lambda account: {
        'deployer_user_arn': f'arn:aws:iam::{account}:user/timex/timex-deployer'})
    with pytest.raises(ValueError, match='not root'):
        cloud.deploy(None, {'Arn': 'arn:aws:iam::123456789012:root'}, '123456789012')


def test_deploy_refuses_stale_source_before_any_cloud_operation(monkeypatch, tmp_path):
    archive = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        for name in APPLICATION_FILES:
            package.writestr(name, '# packaged source\n')
            source = tmp_path / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('# changed source\n')
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    monkeypatch.setattr(cloud, 'ARCHIVE', archive)
    arn = 'arn:aws:iam::123456789012:user/timex/timex-deployer'
    monkeypatch.setattr(cloud, 'load_state', lambda account: {'deployer_user_arn': arn})
    with pytest.raises(ValueError, match='stale'):
        cloud.deploy(None, {'Arn': arn}, '123456789012')
    # An explicit, same-account root override still cannot upload stale code.
    with pytest.raises(ValueError, match='stale'):
        cloud.deploy(None, {'Arn': 'arn:aws:iam::123456789012:root'}, '123456789012', allow_root_bootstrap=True)
    with pytest.raises(ValueError, match='not root'):
        cloud.deploy(None, {'Arn': 'arn:aws:iam::999999999999:root'}, '123456789012', allow_root_bootstrap=True)


def test_temporary_login_cannot_target_another_account(monkeypatch):
    def export(command, **kwargs):
        assert command[command.index('--region') + 1] == 'us-east-1'
        assert kwargs['capture_output'] is True
        return SimpleNamespace(stdout=json.dumps({
            'AccessKeyId': 'synthetic', 'SecretAccessKey': 'synthetic', 'SessionToken': 'synthetic'}))
    monkeypatch.setattr(cloud.subprocess, 'run', export)

    class Session:
        def client(self, service):
            assert service == 'sts'
            return self

        def get_caller_identity(self):
            return {'Account': '111111111111', 'Arn': 'synthetic'}

    monkeypatch.setattr(cloud.boto3, 'Session', lambda **kwargs: Session())
    with pytest.raises(ValueError, match='does not match'):
        cloud.session_for('synthetic-profile', '222222222222')


def test_cloud_smoke_stops_the_billed_session_on_failed_assertion(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    demo = tmp_path / 'examples/demo_request.json'
    demo.parent.mkdir()
    demo.write_text('{}')
    monkeypatch.setattr(cloud, 'load_state', lambda account: {'agentRuntimeArn': 'synthetic'})
    stopped = []

    class Client:
        def invoke_agent_runtime(self, **kwargs):
            assert json.loads(kwargs['payload']) == {'request': {}}
            return {'statusCode': 200, 'response': SimpleNamespace(read=lambda: b'{"status":"partial"}')}

        def stop_runtime_session(self, **kwargs):
            stopped.append(kwargs)

    with pytest.raises(AssertionError):
        cloud.smoke(SimpleNamespace(client=lambda *a, **k: Client()), '123456789012')
    assert len(stopped) == 1
    assert stopped[0]['agentRuntimeArn'] == 'synthetic'


def test_saved_initial_password_has_private_permissions(tmp_path):
    path = tmp_path / 'first-login.json'
    cloud.save(path, {'Password': 'synthetic'}, exclusive=True)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cloud.save(path, {'Password': 'must-not-replace'}, exclusive=True)


def test_cloud_smoke_also_stops_session_after_invoke_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    demo = tmp_path / 'examples/demo_request.json'
    demo.parent.mkdir()
    demo.write_text('{}')
    monkeypatch.setattr(cloud, 'load_state', lambda account: {'agentRuntimeArn': 'synthetic'})
    stopped = []

    class Client:
        def invoke_agent_runtime(self, **kwargs):
            raise TimeoutError('Synthetic connection timeout after request was sent')

        def stop_runtime_session(self, **kwargs):
            stopped.append(kwargs)

    with pytest.raises(TimeoutError):
        cloud.smoke(SimpleNamespace(client=lambda *a, **k: Client()), '123456789012')
    assert len(stopped) == 1
    assert not (tmp_path / 'outputs/aws-runtime-fake-smoke.json').exists()


def test_log_status_reads_only_matching_group_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, 'ROOT', tmp_path)
    monkeypatch.setattr(cloud, 'load_state', lambda account: {'agentRuntimeId': 'timex-synthetic'})

    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {'logGroupNamePrefix': '/aws/bedrock-agentcore/runtimes/timex-synthetic'}
            return [{'logGroups': [{'logGroupName': kwargs['logGroupNamePrefix'] + '-DEFAULT',
                                    'creationTime': 1, 'retentionInDays': 7, 'irrelevant': 'must-not-export'}]}]

    class Logs:
        def get_paginator(self, operation):
            assert operation == 'describe_log_groups'
            return Paginator()

    cloud.log_status(SimpleNamespace(client=lambda name: Logs()), '123456789012')
    saved = json.loads((tmp_path / 'outputs/aws-runtime-log-metadata.json').read_text())
    assert saved == [{'logGroupName': '/aws/bedrock-agentcore/runtimes/timex-synthetic-DEFAULT',
                      'creationTime': 1, 'retentionInDays': 7}]
