import copy
from datetime import date, timedelta
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from timex import gateway, runtime
from timex.gateway_tool import lambda_handler
from timex.models import DeliberationRequest


@pytest.fixture
def request_data():
    return json.loads(Path('examples/demo_request.json').read_text())


def tool_context(gateway_id='synthetic-gateway', name=gateway.TOOL):
    return SimpleNamespace(client_context=SimpleNamespace(custom={
        'bedrockAgentCoreToolName': name, 'bedrockAgentCoreGatewayId': gateway_id}))


@pytest.mark.parametrize('days', [7, 30])
def test_lambda_reuses_normalized_csv_and_labels_synthetic(monkeypatch, days):
    monkeypatch.setenv('TIMEX_GATEWAY_ID', 'synthetic-gateway')
    result = lambda_handler({'period_end': '2026-10-11', 'time_window_days': days}, tool_context())
    summary = result['summary']
    assert summary['period_start'] == (date(2026, 10, 11) - timedelta(days=days - 1)).isoformat()
    assert summary['tracked_minutes'] == 6720
    assert summary['untracked_minutes'] == days * 1440 - 6720
    assert summary['coverage'] == 6720 / (days * 1440)
    assert result['read_only'] is True and result['source'] == 'synthetic_csv'
    assert '合成' in summary['notes'][-1]


@pytest.mark.parametrize('event, context', [
    ({'period_end': '2026-10-11', 'time_window_days': 8}, tool_context()),
    ({'period_end': '2026-10-11', 'time_window_days': 7, 'path': '/private/data.csv'}, tool_context()),
    ({'period_end': '2026-10-11', 'time_window_days': 7}, tool_context('another-gateway')),
    ({'period_end': '2026-10-11', 'time_window_days': 7}, tool_context(name='time-summary___delete_activity')),
])
def test_tool_refuses_unknown_parameters_or_wrong_context(monkeypatch, event, context):
    monkeypatch.setenv('TIMEX_GATEWAY_ID', 'synthetic-gateway')
    assert 'error' in lambda_handler(event, context)


def test_gateway_summary_fetched_once_and_passed_to_council(monkeypatch, request_data):
    monkeypatch.setenv('TIMEX_GATEWAY_URL', 'synthetic-server-setting')
    calls = []
    summary = copy.deepcopy(request_data['time_summary'])
    summary['notes'] = ['合成 Gateway 已冻结快照']

    def rpc(endpoint, method, arguments):
        calls.append((endpoint, method, arguments))
        return {'content': [{'type': 'text', 'text': json.dumps({
            'summary': summary, 'source': 'synthetic_csv', 'read_only': True})}]}

    monkeypatch.setattr(gateway, 'rpc', rpc)
    original = runtime.run_council

    def council(request, **kwargs):
        assert request.time_summary.notes == summary['notes']
        return original(request, **kwargs)

    monkeypatch.setattr(runtime, 'run_council', council)
    body, status = runtime.handle_invocation({'request': request_data, 'summary_source': 'gateway'})
    assert status == 200 and body['status'] == 'complete'
    assert len(calls) == 1 and calls[0][1] == 'tools/call'
    provenance = body['context_sources']['time_summary']
    assert provenance['snapshot'] == summary
    assert provenance['snapshot_sha256'] == gateway.snapshot_hash(summary)
    assert request_data['time_summary']['notes'] != summary['notes']


@pytest.mark.parametrize('failure', ['network', 'different_window', 'malformed'])
def test_gateway_failure_never_silently_uses_input_summary(monkeypatch, request_data, failure):
    monkeypatch.setenv('TIMEX_GATEWAY_URL', 'synthetic-server-setting')

    def broken(*args, **kwargs):
        if failure == 'network':
            raise RuntimeError('PRIVATE_CREDENTIAL_OR_RESPONSE')
        data = {'summary': copy.deepcopy(request_data['time_summary']), 'source': 'synthetic_csv', 'read_only': True}
        if failure == 'different_window':
            data['summary']['timezone'] = 'Asia/Tokyo'
        else:
            data['summary'] = {'private': 'PRIVATE_RECORD'}
        return {'structuredContent': data}

    monkeypatch.setattr(gateway, 'rpc', broken)
    monkeypatch.setattr(runtime, 'run_council', lambda *a, **k: pytest.fail('A failed fetch must stop before deliberation'))
    body, status = runtime.handle_invocation({'request': request_data, 'summary_source': 'gateway'})
    assert status == 503 and body['errors'] == ['gateway_unavailable']
    assert 'PRIVATE' not in json.dumps(body)


@pytest.mark.parametrize('endpoint', ['https://example.com/mcp', 'http://unsafe/mcp',
                                     'https://a.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp?key=private'])
def test_rpc_rejects_non_gateway_urls_before_credential_lookup(endpoint):
    with pytest.raises(ValueError, match='endpoint'):
        gateway.rpc(endpoint, 'tools/list')


def test_lambda_package_has_no_private_or_nonessential_application_files(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('integrations_cloud', Path('scripts/integrations_cloud.py'))
    cloud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cloud)
    dependencies = tmp_path / 'dependencies'
    native = dependencies / 'pydantic_core/native.cpython-312-aarch64-linux-gnu.so'
    native.parent.mkdir(parents=True)
    header = bytearray(20)
    header[:6] = b'\x7fELF\x02\x01'
    header[18:20] = (183).to_bytes(2, 'little')
    native.write_bytes(header)
    (dependencies / '.env').write_text('PRIVATE_CREDENTIAL')
    monkeypatch.setattr(cloud, 'DEPENDENCIES', dependencies)
    archive = cloud.lambda_package(tmp_path / 'lambda.zip')
    with zipfile.ZipFile(archive) as package:
        assert 'examples/demo_events.csv' in package.namelist()
        assert 'timex/gateway_tool.py' in package.namelist()
        assert 'timex/runtime.py' not in package.namelist()
        assert '.env' not in package.namelist()


def test_data_tool_import_does_not_require_graph_module(tmp_path):
    package = tmp_path / 'timex'
    package.mkdir()
    for name in ('__init__.py', 'gateway_tool.py', 'models.py', 'time_data.py'):
        (package / name).write_bytes((Path('timex') / name).read_bytes())
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys; sys.path.insert(0,sys.argv[1]); import timex.gateway_tool; '
        'assert "timex.graph" not in sys.modules; assert "langgraph" not in sys.modules', str(tmp_path)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
