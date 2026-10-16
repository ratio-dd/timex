import json
from pathlib import Path
import stat
import zipfile

from botocore.client import BaseClient
import pytest

from timex.runtime_deploy import APPLICATION_FILES, build_package, deployment_plan


@pytest.fixture
def package_inputs(tmp_path):
    source, dependencies = tmp_path / 'source', tmp_path / 'dependencies'
    for name in APPLICATION_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic package test\n')
    app = dependencies / 'bedrock_agentcore/runtime/app.py'
    app.parent.mkdir(parents=True)
    app.write_text('# synthetic SDK marker\n')
    for name in ('.env', '.aws/credentials', 'data/private/events.csv', 'docs/article.md', 'outputs/result.json'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('PRIVATE_FIXTURE_MUST_NOT_SHIP')
    return source, dependencies, tmp_path / 'runtime.zip'


def elf_arm64():
    header = bytearray(20)
    header[:6] = b'\x7fELF\x02\x01'
    header[18:20] = (183).to_bytes(2, 'little')
    return bytes(header)


def test_package_only_selected_source_and_linux_arm64_dependencies(package_inputs):
    source, dependencies, archive = package_inputs
    native = dependencies / 'native.cpython-312-aarch64-linux-gnu.so'
    native.write_bytes(elf_arm64())
    (dependencies / 'bin').mkdir()
    (dependencies / 'bin/python').write_text('/private/host/python')
    module = dependencies / 'langchain_core/outputs/run_info.py'
    module.parent.mkdir(parents=True)
    module.write_text('# legitimate dependency module\n')
    manifest = build_package(source, dependencies, archive)
    assert manifest['runtime'] == 'PYTHON_3_12'
    assert manifest['native_libraries_checked'] == 1
    with zipfile.ZipFile(archive) as package:
        assert set(APPLICATION_FILES) <= set(package.namelist())
        assert 'bin/python' not in package.namelist()
        assert module.relative_to(dependencies).as_posix() in package.namelist()
        assert not any(name.startswith(('.env', '.aws/', 'data/', 'docs/', 'outputs/'))
                       for name in package.namelist())
        assert all(b'PRIVATE_FIXTURE' not in package.read(name) for name in package.namelist())
        assert all(stat.S_IMODE(info.external_attr >> 16) == 0o644 for info in package.infolist())
    with pytest.raises(ValueError, match='new output'):
        build_package(source, dependencies, archive)


@pytest.mark.parametrize('name, content, message', [
    ('native.so', b'\xcf\xfa\xed\xfe' + b'\x00' * 16, 'Linux ARM64'),
    ('native.so', b'\x7fELF\x02\x01' + b'\x00' * 12 + (62).to_bytes(2, 'little'), 'Linux ARM64'),
    ('native.cpython-311-aarch64-linux-gnu.so', elf_arm64(), 'Python 3.12'),
    ('.env', b'PRIVATE_KEY', 'Unsafe package'),
    ('outputs/result.json', b'PRIVATE_RESULT', 'Unsafe package'),
])
def test_incompatible_or_secret_dependency_is_rejected(package_inputs, name, content, message):
    source, dependencies, archive = package_inputs
    path = dependencies / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    with pytest.raises(ValueError, match=message):
        build_package(source, dependencies, archive)
    assert not archive.exists()


def test_source_symlink_is_rejected(package_inputs):
    source, dependencies, archive = package_inputs
    path = source / 'runtime_entrypoint.py'
    path.unlink()
    path.symlink_to(source / '.env')
    with pytest.raises(ValueError, match='symbolic link'):
        build_package(source, dependencies, archive)


def test_preflight_is_offline_and_uses_safe_server_defaults(monkeypatch, package_inputs):
    source, dependencies, archive = package_inputs
    build_package(source, dependencies, archive)

    def forbidden(*args, **kwargs):
        raise AssertionError('Local preflight must not call AWS')

    monkeypatch.setattr(BaseClient, '_make_api_call', forbidden)
    plan = deployment_plan(archive, account_id='123456789012')
    assert plan['aws_calls_made'] == 0
    assert plan['region'] == 'us-east-1'
    assert plan['status'] == 'local_package_ready_cloud_preflight_pending'
    params = plan['create_agent_runtime_request_template']
    assert params['agentRuntimeArtifact']['codeConfiguration']['runtime'] == 'PYTHON_3_12'
    assert params['agentRuntimeArtifact']['codeConfiguration']['entryPoint'] == ['runtime_entrypoint.py']
    assert params['lifecycleConfiguration'] == {'idleRuntimeSessionTimeout': 60, 'maxLifetime': 600}
    assert params['environmentVariables']['TIMEX_ALLOW_LIVE'] == '0'
    assert 'OPENAI_API_KEY' not in params['environmentVariables']
    assert params['environmentVariables']['MAX_ESTIMATED_USD_PER_RUN'] == '0.05'
    assert plan['runtime_role_trust_template']['Statement'][0]['Condition']['StringEquals'] == {
        'aws:SourceAccount': '123456789012'}
    assert plan['deployer_pass_role_template']['Resource'] == params['roleArn']
    assert 'AdministratorAccess' not in json.dumps(plan)


def test_preflight_keeps_missing_account_explicit(package_inputs):
    source, dependencies, archive = package_inputs
    build_package(source, dependencies, archive)
    plan = deployment_plan(archive)
    assert '${ACCOUNT_ID}' in plan['create_agent_runtime_request_template']['roleArn']
    assert plan['pending_before_any_deployment']
    with pytest.raises(ValueError, match='12 digits'):
        deployment_plan(archive, account_id='root')
