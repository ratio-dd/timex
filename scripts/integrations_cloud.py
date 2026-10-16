"""Deploy and verify the synthetic Gateway/Memory integrations with temporary CLI credentials."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
import uuid
import zipfile
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_cloud import ROOT, REGION, TAGS, load_state as runtime_state, require_owned, save, session_for
from timex.gateway import rpc, TOOL, VERSION
from timex.runtime_deploy import APPLICATION_FILES, inspect_package

STATE = ROOT / 'outputs/aws-integrations-state.json'
DEPENDENCIES = ROOT / 'build/runtime-dependencies-py312-arm64-glibc228'


def read_api_key(path):
    """Read one local key without executing dotenv text or exposing other fields."""
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('The local key file must be a regular private file (mode 600)')
    values = []
    for line in path.read_text().splitlines():
        name, separator, value = line.strip().removeprefix('export ').partition('=')
        if separator and name.strip() in {'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}:
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError('Invalid local API key field') from None
            if len(parts) != 1 or not parts[0] or any(char.isspace() for char in parts[0]):
                raise ValueError('Invalid local API key field')
            values.append(parts[0])
    if not values or len(set(values)) != 1:
        raise ValueError('Supply one unambiguous local API key')
    return values[0]


def upload_secret(session, account, path):
    state = state_for(account)
    original = runtime_state(account)
    control = session.client('bedrock-agentcore-control')
    current = control.get_agent_runtime(agentRuntimeId=original['agentRuntimeId'])
    require_owned(control.list_tags_for_resource(resourceArn=current['agentRuntimeArn'])['tags'])
    if (current['environmentVariables'].get('TIMEX_ALLOW_LIVE') != '0'
            or current['environmentVariables'].get('MODEL_PROVIDER') != 'fake'):
        raise ValueError('Secret preparation requires the existing fake-only Runtime')
    iam = session.client('iam')
    require_owned(iam.get_role(RoleName='timex-runtime')['Role'].get('Tags', []))
    key = read_api_key(path)
    client = session.client('secretsmanager')
    try:
        metadata = client.describe_secret(SecretId='timex/deepseek')
        require_owned(metadata.get('Tags', []))
        if metadata.get('DeletedDate'):
            raise ValueError('The project secret is scheduled for deletion')
    except client.exceptions.ResourceNotFoundException:
        result = client.create_secret(Name='timex/deepseek',
            Description='TimeX DeepSeek credential; live model execution remains disabled',
            SecretString=json.dumps({'OPENAI_API_KEY': key}),
            Tags=[{'Key': k, 'Value': v} for k, v in TAGS.items()])
    else:
        result = client.put_secret_value(SecretId=metadata['ARN'],
                                         SecretString=json.dumps({'OPENAI_API_KEY': key}))
    # Compare in memory; neither the value nor its digest is written to evidence.
    stored = client.get_secret_value(SecretId=result['ARN'], VersionId=result['VersionId'])
    if json.loads(stored['SecretString']) != {'OPENAI_API_KEY': key}:
        raise ValueError('Stored credential verification failed')
    permissions = policy(['secretsmanager:GetSecretValue'], result['ARN'])
    iam.put_role_policy(RoleName='timex-runtime', PolicyName='timex-deepseek-secret',
                        PolicyDocument=json.dumps(permissions))
    record(state, deepseek_secret_arn=result['ARN'], deepseek_secret_version=result['VersionId'])
    save(ROOT / 'outputs/aws-deepseek-secret-permissions.json', permissions)
    report = {'status': 'stored_and_verified', 'secret_name': 'timex/deepseek',
              'secret_arn': result['ARN'], 'version_id': result['VersionId'],
              'stored_value_matches_local': True, 'paid_model_calls': 0,
              'live_enabled': False, 'encryption': 'AWS managed aws/secretsmanager key',
              'runtime_link': 'run runtime-update to configure only the ARN',
              'verified_at': datetime.now(timezone.utc).isoformat()}
    save(ROOT / 'outputs/aws-deepseek-secret-upload.json', report)
    print(json.dumps(report, indent=2))


def state_for(account):
    state = json.loads(STATE.read_text()) if STATE.exists() else {'account': account, 'region': REGION}
    if state['account'] != account or state['region'] != REGION:
        raise ValueError('Integration state belongs to a different account/region')
    return state


def record(state, **values):
    state.update(values, updated_at=datetime.now(timezone.utc).isoformat())
    save(STATE, state)


def policy(actions, resource):
    return {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': actions, 'Resource': resource}]}


def role(session, account, name, service, permissions, source=None):
    iam = session.client('iam')
    trust = policy(['sts:AssumeRole'], '*')
    statement = trust['Statement'][0]
    statement.pop('Resource')
    statement['Principal'] = {'Service': service}
    if source:
        statement['Condition'] = {'StringEquals': {'aws:SourceAccount': account},
                                  'ArnLike': {'aws:SourceArn': source}}
    try:
        current = iam.get_role(RoleName=name)['Role']
        require_owned(current.get('Tags', []))
    except iam.exceptions.NoSuchEntityException:
        current = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                                  Tags=[{'Key': k, 'Value': v} for k, v in TAGS.items()])['Role']
    iam.put_role_policy(RoleName=name, PolicyName=name + '-permissions', PolicyDocument=json.dumps(permissions))
    return current['Arn']


def wait_ready(get, ready='READY', timeout=180):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        result = get()
        state = result['status']
        if state != previous:
            print('Resource status:', state, flush=True)
            previous = state
        if state == ready:
            return result
        if state in {'FAILED', 'CREATE_FAILED', 'UPDATE_FAILED', 'DELETING', 'DELETED'}:
            raise RuntimeError('Resource failed: ' + json.dumps(result, default=str))
        time.sleep(5)
    raise TimeoutError('Resource did not become ready within the bounded wait')


def lambda_package(output):
    files = {name: ROOT / name for name in ('timex/__init__.py', 'timex/models.py', 'timex/time_data.py',
                                           'timex/gateway_tool.py', 'examples/demo_events.csv')}
    for module in ('pydantic', 'pydantic_core', 'annotated_types', 'typing_inspection', 'typing_extensions.py'):
        path = DEPENDENCIES / module
        for file in ([path] if path.is_file() else path.rglob('*')):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                files[file.relative_to(DEPENDENCIES).as_posix()] = file
    if not any(name.endswith('.so') for name in files):
        raise ValueError('Locked ARM64 dependencies are missing')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, file in sorted(files.items()):
            if file.is_symlink():
                raise ValueError('Package symlinks are forbidden')
            data = file.read_bytes()
            if name.endswith('.so') and (data[:4] != b'\x7fELF' or int.from_bytes(data[18:20], 'little') != 183
                                         or 'cpython-312-' not in name):
                raise ValueError('Lambda native dependencies must target Linux ARM64 Python 3.12')
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output


def create_lambda(client, **parameters):
    for attempt in range(4):
        try:
            return client.create_function(**parameters)
        except ClientError as error:
            if (error.response['Error']['Code'] != 'InvalidParameterValueException'
                    or 'cannot be assumed by Lambda' not in error.response['Error']['Message'] or attempt == 3):
                raise
            time.sleep(3)


def deploy_gateway(session, account):
    state = state_for(account)
    control = session.client('bedrock-agentcore-control')
    function_arn = f'arn:aws:lambda:{REGION}:{account}:function:timex-summary-tool'
    gateway_role = role(session, account, 'timex-gateway', 'bedrock-agentcore.amazonaws.com',
                        policy(['lambda:InvokeFunction'], function_arn),
                        f'arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/timex-*')
    if 'gateway_id' not in state:
        existing = []
        for page in control.get_paginator('list_gateways').paginate():
            existing.extend(item for item in page['items'] if item['name'] == 'timex-summary')
        if existing:
            require_owned(control.list_tags_for_resource(resourceArn=existing[0]['gatewayArn'])['tags'])
            gateway = control.get_gateway(gatewayIdentifier=existing[0]['gatewayId'])
        else:
            gateway = control.create_gateway(name='timex-summary', roleArn=gateway_role, protocolType='MCP',
                                              authorizerType='AWS_IAM', protocolConfiguration={'mcp': {
                                                  'supportedVersions': [VERSION]}}, tags=TAGS,
                                              description='One read-only synthetic time-summary tool; no inference routes')
        record(state, gateway_id=gateway['gatewayId'], gateway_arn=gateway['gatewayArn'],
               gateway_url=gateway['gatewayUrl'], gateway_role_arn=gateway_role)
    wait_ready(lambda: control.get_gateway(gatewayIdentifier=state['gateway_id']))
    log_arn = f'arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/timex-summary-tool'
    permissions = policy(['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'], [log_arn, log_arn + ':*'])
    lambda_role = role(session, account, 'timex-summary-lambda', 'lambda.amazonaws.com', permissions)
    client = session.client('lambda')
    package = lambda_package(ROOT / ('build/timex-summary-lambda-' + uuid.uuid4().hex[:10] + '.zip'))
    package_bytes = package.read_bytes()
    try:
        function = client.get_function(FunctionName='timex-summary-tool')
        require_owned(function.get('Tags', {}))
        if function['Configuration']['CodeSha256'] != base64.b64encode(hashlib.sha256(package_bytes).digest()).decode():
            client.update_function_code(FunctionName='timex-summary-tool', ZipFile=package_bytes)
            client.get_waiter('function_updated_v2').wait(FunctionName='timex-summary-tool',
                                                       WaiterConfig={'Delay': 3, 'MaxAttempts': 40})
    except client.exceptions.ResourceNotFoundException:
        create_lambda(client, FunctionName='timex-summary-tool', Runtime='python3.12', Architectures=['arm64'],
                               Role=lambda_role, Handler='timex.gateway_tool.lambda_handler',
                               Code={'ZipFile': package_bytes}, Timeout=15, MemorySize=256, Tags=TAGS,
                               Environment={'Variables': {'TIMEX_GATEWAY_ID': state['gateway_id']}})
    client.get_waiter('function_active_v2').wait(FunctionName='timex-summary-tool',
                                               WaiterConfig={'Delay': 3, 'MaxAttempts': 40})
    record(state, summary_lambda_arn=function_arn, summary_lambda_role_arn=lambda_role)
    if 'gateway_target_id' not in state:
        query = {'name': 'get_time_summary', 'description': 'Read synthetic demo CSV as a 7/30 day summary; no writes',
                 'inputSchema': {'type': 'object', 'properties': {
                     'period_end': {'type': 'string'}, 'time_window_days': {'type': 'integer'},
                     'timezone': {'type': 'string'}}, 'required': ['period_end', 'time_window_days']}}
        target = control.create_gateway_target(gatewayIdentifier=state['gateway_id'], name='time-summary',
                    targetConfiguration={'mcp': {'lambda': {'lambdaArn': function_arn,
                        'toolSchema': {'inlinePayload': [query]}}}},
                    credentialProviderConfigurations=[{'credentialProviderType': 'GATEWAY_IAM_ROLE'}])
        record(state, gateway_target_id=target['targetId'])
    wait_ready(lambda: control.get_gateway_target(gatewayIdentifier=state['gateway_id'], targetId=state['gateway_target_id']))
    session.client('iam').put_role_policy(RoleName='timex-runtime', PolicyName='timex-gateway-invoke',
        PolicyDocument=json.dumps(policy(['bedrock-agentcore:InvokeGateway'], state['gateway_arn'])))
    save(ROOT / 'outputs/aws-gateway-permissions.json', {
        'gateway_role': policy(['lambda:InvokeFunction'], function_arn),
        'runtime_role': policy(['bedrock-agentcore:InvokeGateway'], state['gateway_arn']),
        'lambda_role': permissions})
    print(json.dumps(state, indent=2))


def update_runtime(session, account, archive):
    state = state_for(account)
    original = runtime_state(account)
    manifest = inspect_package(archive)
    with zipfile.ZipFile(archive) as package:
        if any(package.read(name) != (ROOT / name).read_bytes() for name in APPLICATION_FILES):
            raise ValueError('Archive does not match current application source')
    control = session.client('bedrock-agentcore-control')
    current = control.get_agent_runtime(agentRuntimeId=original['agentRuntimeId'])
    require_owned(control.list_tags_for_resource(resourceArn=current['agentRuntimeArn'])['tags'])
    if current['environmentVariables'].get('TIMEX_ALLOW_LIVE') != '0':
        raise ValueError('Expected the existing fake-only Runtime')
    parameters = {key: current[key] for key in ('roleArn', 'networkConfiguration', 'protocolConfiguration',
                                               'lifecycleConfiguration', 'environmentVariables')}
    parameters['agentRuntimeId'] = original['agentRuntimeId']
    key = 'timex/' + manifest['sha256'] + '.zip'
    session.client('s3').upload_file(str(archive), original['bucket'], key,
                                     ExtraArgs={'ExpectedBucketOwner': account, 'ServerSideEncryption': 'AES256'})
    parameters['agentRuntimeArtifact'] = {'codeConfiguration': {
        'code': {'s3': {'bucket': original['bucket'], 'prefix': key}},
        'runtime': 'PYTHON_3_12', 'entryPoint': ['runtime_entrypoint.py']}}
    parameters['environmentVariables'].update(TIMEX_GATEWAY_URL=state['gateway_url'],
                                              TIMEX_ALLOW_LIVE='0', MODEL_PROVIDER='fake')
    if 'memory_id' in state:
        parameters['environmentVariables'].update(TIMEX_MEMORY_ID=state['memory_id'],
                                                  TIMEX_MEMORY_ACTOR_ID='timex-synthetic-demo')
    if 'deepseek_secret_arn' in state:
        parameters['environmentVariables']['TIMEX_DEEPSEEK_SECRET_ARN'] = state['deepseek_secret_arn']
    response = control.update_agent_runtime(**parameters)
    original.update(agentRuntimeVersion=response['agentRuntimeVersion'], status=response['status'],
                     artifact=manifest, s3_object=key, updated_at=datetime.now(timezone.utc).isoformat())
    save(ROOT / 'outputs/aws-runtime-state.json', original)
    save(ROOT / 'outputs/aws-runtime-integration-update.json', parameters)
    ready = wait_ready(lambda: control.get_agent_runtime(agentRuntimeId=original['agentRuntimeId']))
    endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=original['agentRuntimeId'], endpointName='DEFAULT')
    if endpoint['liveVersion'] != response['agentRuntimeVersion']:
        control.update_agent_runtime_endpoint(agentRuntimeId=original['agentRuntimeId'], endpointName='DEFAULT',
                                               agentRuntimeVersion=response['agentRuntimeVersion'])
        wait_ready(lambda: control.get_agent_runtime_endpoint(agentRuntimeId=original['agentRuntimeId'], endpointName='DEFAULT'))
    original.update(status=ready['status'], phase='runtime_ready', updated_at=datetime.now(timezone.utc).isoformat())
    save(ROOT / 'outputs/aws-runtime-state.json', original)
    record(state, runtime_version=response['agentRuntimeVersion'])
    print('Runtime integration version:', response['agentRuntimeVersion'])


def invoke(session, account, payload):
    from botocore.config import Config
    original = runtime_state(account)
    client = session.client('bedrock-agentcore', config=Config(read_timeout=180, retries={'total_max_attempts': 1}))
    sid = str(uuid.uuid4())
    try:
        response = client.invoke_agent_runtime(agentRuntimeArn=original['agentRuntimeArn'], runtimeSessionId=sid,
                     qualifier='DEFAULT', contentType='application/json', accept='application/json',
                     payload=json.dumps(payload).encode())
        body = json.loads(response['response'].read())
        if response['statusCode'] != 200:
            raise ValueError('Runtime returned a non-success response')
        return body, {'session_id': sid, 'aws_request_id': response['ResponseMetadata']['RequestId']}
    finally:
        client.stop_runtime_session(agentRuntimeArn=original['agentRuntimeArn'], runtimeSessionId=sid, qualifier='DEFAULT')


def gateway_smoke(session, account):
    state = state_for(account)
    tools = rpc(state['gateway_url'], 'tools/list', session=session)
    assert [item['name'] for item in tools['tools']] == [TOOL]
    denial = rpc(state['gateway_url'], 'tools/list', signed=False)
    assert denial['http_status'] in (401, 403)
    request = json.loads((ROOT / 'examples/demo_request.json').read_text())
    result, meta = invoke(session, account, {'request': request, 'summary_source': 'gateway'})
    assert result['status'] == 'complete' and result['usage']['provider'] == 'fake'
    assert result['usage']['model_calls'] == 9 and result['usage']['estimated_usd'] == 0
    provenance = result['context_sources']['time_summary']
    assert provenance['source'] == 'aws_gateway' and provenance['tool_calls'] == 1
    save(ROOT / 'outputs/aws-gateway-council.json', result)
    report = {'status': 'passed', 'gateway_id': state['gateway_id'], 'protocol': VERSION,
              'gateway_authorization': 'AWS_IAM', 'unsigned_request_status': denial['http_status'],
              'time_summary': provenance, 'runtime': meta, 'paid_model_calls': 0, 'rule_calls': 9,
              'sessions_stopped': True, 'synthetic_only': True,
              'cleanup_order': ['Gateway target', 'Gateway', 'summary Lambda', 'integration role policies/roles'],
              'sources': ['https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html',
                          'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html']}
    save(ROOT / 'outputs/aws-gateway-smoke.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'unsigned_request_status', 'paid_model_calls', 'rule_calls')}, indent=2))


def deploy_memory(session, account):
    state = state_for(account)
    control = session.client('bedrock-agentcore-control')
    if 'memory_id' not in state:
        existing = []
        for page in control.get_paginator('list_memories').paginate():
            existing.extend(item for item in page['memories'] if item['id'].startswith('timex_preferences-'))
        if existing:
            if len(existing) != 1:
                raise ValueError('Multiple matching Memory resources require review')
            require_owned(control.list_tags_for_resource(resourceArn=existing[0]['arn'])['tags'])
            memory = control.get_memory(memoryId=existing[0]['id'])['memory']
        else:
            response = control.create_memory(name='timex_preferences', eventExpiryDuration=7, memoryStrategies=[],
                description='Synthetic confirmed preferences as short-term events; no automatic extraction', tags=TAGS,
                clientToken=str(uuid.uuid5(uuid.NAMESPACE_URL, account + ':timex_preferences:events-only')))
            memory = response['memory']
        record(state, memory_id=memory['id'], memory_arn=memory['arn'])
    memory = wait_ready(lambda: control.get_memory(memoryId=state['memory_id'])['memory'], ready='ACTIVE')
    if memory.get('strategies') or memory.get('memoryExecutionRoleArn'):
        raise ValueError('Expected short-term-only Memory without automatic extraction or execution role')
    permissions = policy(['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents',
                          'bedrock-agentcore:GetEvent', 'bedrock-agentcore:DeleteEvent'], state['memory_arn'])
    session.client('iam').put_role_policy(RoleName='timex-runtime', PolicyName='timex-confirmed-memory',
                                           PolicyDocument=json.dumps(permissions))
    record(state, memory_mode='short_term_events', memory_event_retention_days=memory['eventExpiryDuration'],
           memory_extraction_strategies=[])
    save(ROOT / 'outputs/aws-memory-permissions.json', permissions)
    print(json.dumps({key: state[key] for key in ('memory_id', 'memory_mode', 'memory_event_retention_days',
                                                  'memory_extraction_strategies')}, indent=2))


def memory_smoke(session, account):
    state = state_for(account)
    request = json.loads((ROOT / 'examples/demo_request.json').read_text())
    phases = []

    def council(label, available=None):
        data = dict(request)
        if available is not None:
            data['available_minutes'] = available
        result, meta = invoke(session, account, {'request': data, 'summary_source': 'gateway', 'use_memory': True})
        assert result['status'] == 'complete' and result['usage']['provider'] == 'fake'
        assert result['usage']['model_calls'] == 9 and result['usage']['estimated_usd'] == 0
        assert result['context_sources']['time_summary']['tool_calls'] == 1
        memory = result['context_sources']['memory']
        amounts = {role: value['suggested_minutes'] for role, value in result['round1'].items()}
        phases.append({'phase': label, 'memory': memory, 'round1_minutes': amounts,
                       'summary_sha256': result['context_sources']['time_summary']['snapshot_sha256'], **meta})
        save(ROOT / f'outputs/aws-memory-{label}-council.json', result)
        return memory, amounts

    before, before_amounts = council('before')
    assert before['preference'] is None
    written, write_meta = invoke(session, account, {'operation': 'confirm_preference', 'confirmed': True,
        'preference': {'text': '合成用户确认：TimeX 后续会议默认最多新增投入30分钟，当前明确要求可覆盖。', 'maximum_minutes': 30}})
    assert written['status'] == 'complete'
    after, amounts = council('after-confirm')
    assert after['event_id'] == written['memory']['event_id'] and after['applied'] is True
    assert all(value <= 30 for value in amounts.values()) and amounts != before_amounts
    override, override_amounts = council('current-override', 60)
    assert override['precedence'] == 'current_request' and override['conflict'] is True
    assert all(value <= 60 for value in override_amounts.values()) and max(override_amounts.values()) > 30
    corrected, correct_meta = invoke(session, account, {'operation': 'correct_preference', 'confirmed': True,
        'preference': {'text': '合成用户更正：TimeX 后续会议默认最多新增投入45分钟，当前明确要求可覆盖。', 'maximum_minutes': 45}})
    assert corrected['status'] == 'complete' and corrected['deleted_event_count'] == 1
    recalled, corrected_amounts = council('after-correct')
    assert recalled['event_id'] == corrected['memory']['event_id']
    assert recalled['preference']['maximum_minutes'] == 45 and all(v <= 45 for v in corrected_amounts.values())
    deleted, delete_meta = invoke(session, account, {'operation': 'delete_preference', 'confirmed': True})
    assert deleted['status'] == 'complete' and deleted['deleted_event_count'] == 1
    absent, restored_amounts = council('after-delete')
    assert absent['preference'] is None and restored_amounts == before_amounts
    assert len({phase['session_id'] for phase in phases}) == 5
    assert len({phase['summary_sha256'] for phase in phases}) == 1
    report = {'status': 'passed', 'memory_id': state['memory_id'], 'mode': 'short_term_confirmed_events',
              'automatic_extraction': False, 'long_term_memory_claimed': False, 'event_retention_days': 7,
              'actor_binding': 'trusted server configuration; single synthetic demo actor',
              'precedence_boundary': 'explicit structured available_minutes overrides remembered default',
              'phases': phases, 'write': {'event_id': written['memory']['event_id'], **write_meta},
              'correction': {'event_id': corrected['memory']['event_id'], **correct_meta},
              'deletion': {'deleted_event_count': deleted['deleted_event_count'], **delete_meta},
              'test_preference_events_remaining': 0, 'sessions_stopped': True,
              'council_runs': 5, 'rule_calls': 45, 'paid_model_calls': 0, 'synthetic_only': True,
              'aws_cost': 'not measured; normal Gateway, Lambda, Runtime and Memory charges apply',
              'sources': ['https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-create-a-memory-store.html',
                          'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/using-memory-short-term.html']}
    save(ROOT / 'outputs/aws-memory-smoke.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'memory_id', 'mode', 'council_runs',
                                                  'paid_model_calls', 'test_preference_events_remaining')}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['gateway-deploy', 'runtime-update', 'gateway-smoke',
                                            'memory-deploy', 'memory-smoke', 'secret-upload'])
    parser.add_argument('--profile', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--env-file', type=Path, help='Private local dotenv file; read only by secret-upload')
    args = parser.parse_args()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    session, identity = session_for(args.profile, args.account_id)
    if identity['Arn'] != f'arn:aws:iam::{args.account_id}:root':
        raise ValueError('This integration bootstrap expects the explicitly authorized temporary root session')
    if args.command == 'gateway-deploy':
        deploy_gateway(session, args.account_id)
    elif args.command == 'runtime-update':
        if not args.archive:
            parser.error('--archive is required')
        update_runtime(session, args.account_id, args.archive)
    elif args.command == 'gateway-smoke':
        gateway_smoke(session, args.account_id)
    elif args.command == 'memory-deploy':
        deploy_memory(session, args.account_id)
    elif args.command == 'secret-upload':
        if not args.env_file:
            parser.error('--env-file is required')
        try:
            upload_secret(session, args.account_id, args.env_file)
        except Exception:
            parser.exit(1, 'Secret upload failed; values suppressed. Check authorization, file permissions and project ownership.\n')
    else:
        memory_smoke(session, args.account_id)


if __name__ == '__main__':
    main()
