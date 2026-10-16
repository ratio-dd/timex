"""One bounded, IAM-signed MCP request to the deployment-selected Gateway."""
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
import uuid

from .models import TimeSummary

VERSION = '2026-07-28'
TOOL = 'time-summary___get_time_summary'


def snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def rpc(endpoint, method, arguments=None, *, session=None, signed=True):
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import requests

    region = os.getenv('AWS_REGION', 'us-east-1')
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or url.path != '/mcp' or url.query or url.fragment or url.username or url.password
            or not re.fullmatch(r'[a-z0-9-]+\.gateway\.bedrock-agentcore\.' + re.escape(region)
                                + r'\.amazonaws\.com', url.netloc)):
        raise ValueError('invalid deployment Gateway endpoint')
    params = {'_meta': {'io.modelcontextprotocol/protocolVersion': VERSION,
                        'io.modelcontextprotocol/clientInfo': {'name': 'timex', 'version': '0.1.0'},
                        'io.modelcontextprotocol/clientCapabilities': {}}}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': VERSION, 'Mcp-Method': method}
    if method == 'tools/call':
        params.update(name=TOOL, arguments=arguments)
        headers['Mcp-Name'] = TOOL
    elif method != 'tools/list':
        raise ValueError('unsupported MCP method')
    payload = json.dumps({'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': method,
                          'params': params}).encode()
    if signed:
        credentials = (session or boto3.Session()).get_credentials().get_frozen_credentials()
        request = AWSRequest(method='POST', url=endpoint, data=payload, headers=headers)
        SigV4Auth(credentials, 'bedrock-agentcore', region).add_auth(request)
        headers = dict(request.headers)
    with requests.Session() as client:
        client.trust_env = False
        response = client.post(endpoint, data=payload, headers=headers, timeout=(5, 20), allow_redirects=False)
    if not signed:
        return {'http_status': response.status_code}
    response.raise_for_status()
    if len(response.content) > 1024 * 1024:
        raise ValueError('Gateway response exceeds limit')
    if response.headers.get('Content-Type', '').startswith('text/event-stream'):
        messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        envelope = next(item for item in messages if 'result' in item or 'error' in item)
    else:
        envelope = response.json()
    if 'error' in envelope or envelope['result'].get('isError'):
        raise ValueError('Gateway tool failed')
    return envelope['result']


def fetch_summary(request):
    endpoint = os.environ['TIMEX_GATEWAY_URL']
    result = rpc(endpoint, 'tools/call', {'period_end': request.time_summary.period_end.isoformat(),
                                        'time_window_days': request.time_window_days,
                                        'timezone': request.time_summary.timezone})
    data = result.get('structuredContent')
    if data is None:
        data = json.loads(next(item['text'] for item in result['content'] if item['type'] == 'text'))
    if data.get('source') != 'synthetic_csv' or data.get('read_only') is not True:
        raise ValueError('unexpected Gateway data source')
    summary = TimeSummary.model_validate(data['summary'])
    if (summary.period_end != request.time_summary.period_end or summary.timezone != request.time_summary.timezone
            or (summary.period_end - summary.period_start).days + 1 != request.time_window_days):
        raise ValueError('Gateway returned a different time window')
    value = summary.model_dump(mode='json')
    return summary, {'source': 'aws_gateway', 'tool': TOOL, 'tool_calls': 1,
                     'synthetic': True, 'snapshot_sha256': snapshot_hash(value), 'snapshot': value}
