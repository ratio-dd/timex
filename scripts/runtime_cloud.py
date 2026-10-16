"""Explicit AWS bootstrap, deployment and fake smoke commands for the TimeX Runtime.

Run with .venv/bin/python scripts/runtime_cloud.py COMMAND --profile NAME
--account-id ID. Credentials come from an existing AWS CLI temporary login.
No commands run on import. This script never reads .env or invokes a paid model.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timex.runtime_deploy import APPLICATION_FILES, deployment_plan


ROOT = Path(__file__).resolve().parents[1]
REGION = 'us-east-1'
TAGS = {'Project': 'TimeX', 'ManagedBy': 'timex-runtime-local'}
STATE = ROOT / 'outputs/aws-runtime-state.json'
ARCHIVE = ROOT / 'build/timex-runtime-py312-arm64.zip'


def save(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x' if exclusive else 'w', encoding='utf-8') as output:
        os.chmod(path, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2, default=str)
        output.write('\n')


def session_for(profile, account):
    # Export into memory only: boto3 needs no long-lived keys or optional CRT.
    credentials = subprocess.run(
        ['aws', 'configure', 'export-credentials', '--profile', profile,
         '--region', REGION, '--format', 'process'],
        check=True, capture_output=True, text=True)
    value = json.loads(credentials.stdout)
    session = boto3.Session(aws_access_key_id=value['AccessKeyId'],
                            aws_secret_access_key=value['SecretAccessKey'],
                            aws_session_token=value['SessionToken'], region_name=REGION)
    identity = session.client('sts').get_caller_identity()
    if identity['Account'] != account:
        raise ValueError('Authenticated account does not match --account-id')
    return session, identity


def require_owned(tags):
    if isinstance(tags, list):
        tags = {item['Key']: item['Value'] for item in tags}
    if any(tags.get(key) != value for key, value in TAGS.items()):
        raise ValueError('Existing resource is not owned by this deployment; refusing changes')


def policies(account, plan):
    prefix = f'arn:aws:bedrock-agentcore:{REGION}:{account}'
    runtime = prefix + ':runtime/timex-*'
    bucket = f'timex-runtime-{account}-{REGION}'
    logs = f'arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/runtimes/timex-*'
    statement = lambda actions, resources: {'Effect': 'Allow', 'Action': actions, 'Resource': resources}
    deployer = {'Version': '2012-10-17', 'Statement': [
        statement(['bedrock-agentcore:CreateAgentRuntime', 'bedrock-agentcore:GetAgentRuntime',
                   'bedrock-agentcore:UpdateAgentRuntime', 'bedrock-agentcore:DeleteAgentRuntime',
                   'bedrock-agentcore:CreateAgentRuntimeEndpoint', 'bedrock-agentcore:GetAgentRuntimeEndpoint',
                   'bedrock-agentcore:ListAgentRuntimeEndpoints', 'bedrock-agentcore:ListAgentRuntimeVersions',
                   'bedrock-agentcore:UpdateAgentRuntimeEndpoint', 'bedrock-agentcore:DeleteAgentRuntimeEndpoint',
                   'bedrock-agentcore:InvokeAgentRuntime', 'bedrock-agentcore:StopRuntimeSession',
                   'bedrock-agentcore:TagResource', 'bedrock-agentcore:ListTagsForResource'], [runtime, runtime + '/*']),
        statement(['bedrock-agentcore:ListAgentRuntimes'], '*'),
        statement(['bedrock-agentcore:CreateWorkloadIdentity', 'bedrock-agentcore:GetWorkloadIdentity',
                   'bedrock-agentcore:DeleteWorkloadIdentity', 'bedrock-agentcore:TagResource'],
                  [prefix + ':workload-identity-directory/default',
                   prefix + ':workload-identity-directory/default/workload-identity/timex*']),
        plan['deployer_pass_role_template'],
        statement(['s3:ListBucket', 's3:GetBucketLocation'], f'arn:aws:s3:::{bucket}'),
        statement(['s3:PutObject', 's3:GetObject', 's3:DeleteObject'], f'arn:aws:s3:::{bucket}/timex/*'),
        statement(['logs:DescribeLogGroups'], f'arn:aws:logs:{REGION}:{account}:log-group:*'),
        statement(['logs:GetLogEvents', 'logs:FilterLogEvents', 'logs:DescribeLogStreams',
                   'logs:PutRetentionPolicy'], [logs, logs + ':*']),
        statement(['iam:ChangePassword'], f'arn:aws:iam::{account}:user/timex/timex-deployer'),
        statement(['iam:GetAccountPasswordPolicy'], '*'),
        statement(['signin:AuthorizeOAuth2Access', 'signin:CreateOAuth2Token'],
                  f'arn:aws:signin:*:{account}:oauth2/public-client/*'),
    ]}
    execution = {'Version': '2012-10-17', 'Statement': [
        statement(['s3:GetObject'], f'arn:aws:s3:::{bucket}/timex/*'),
        statement(['logs:DescribeLogGroups'], f'arn:aws:logs:{REGION}:{account}:log-group:*'),
        statement(['logs:CreateLogGroup', 'logs:DescribeLogStreams', 'logs:PutResourcePolicy'], [logs, logs + ':*']),
        statement(['logs:CreateLogStream', 'logs:PutLogEvents'], logs + ':log-stream:*'),
    ]}
    return deployer, execution


def bootstrap(session, identity, account):
    state = load_state(account) if STATE.exists() else {}
    plan = deployment_plan(ARCHIVE, account_id=account)
    deployer, execution = policies(account, plan)
    iam = session.client('iam')
    tags = [{'Key': key, 'Value': value} for key, value in TAGS.items()]
    role_name, user_name = 'timex-runtime', 'timex-deployer'
    try:
        role = iam.get_role(RoleName=role_name)['Role']
        require_owned(role.get('Tags', []))
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(plan['runtime_role_trust_template']),
                               Description='TimeX Runtime execution only; no model invocation permissions', Tags=tags)['Role']
    iam.put_role_policy(RoleName=role_name, PolicyName='timex-runtime-execution', PolicyDocument=json.dumps(execution))
    try:
        user = iam.get_user(UserName=user_name)['User']
        require_owned(user.get('Tags', []))
    except iam.exceptions.NoSuchEntityException:
        user = iam.create_user(UserName=user_name, Path='/timex/', Tags=tags)['User']
    policy_arn = f'arn:aws:iam::{account}:policy/timex/runtime-deployer'
    try:
        iam.get_policy(PolicyArn=policy_arn)
        require_owned(iam.list_policy_tags(PolicyArn=policy_arn)['Tags'])
        existing = iam.get_policy(PolicyArn=policy_arn)['Policy']
        document = iam.get_policy_version(PolicyArn=policy_arn, VersionId=existing['DefaultVersionId'])['PolicyVersion']['Document']
        if document != deployer:
            iam.create_policy_version(PolicyArn=policy_arn, PolicyDocument=json.dumps(deployer), SetAsDefault=True)
    except iam.exceptions.NoSuchEntityException:
        iam.create_policy(PolicyName='runtime-deployer', Path='/timex/', PolicyDocument=json.dumps(deployer), Tags=tags)
    iam.attach_user_policy(UserName=user_name, PolicyArn=policy_arn)
    login_file = ROOT / 'outputs/aws-timex-first-login.json'
    try:
        iam.get_login_profile(UserName=user_name)
    except iam.exceptions.NoSuchEntityException:
        password = 'Aa1!' + ''.join(secrets.choice(string.ascii_letters + string.digits + '!@#%_-') for _ in range(40))
        # Preserve the initial password locally before the API call; never print it.
        save(login_file, {'Account': account, 'UserName': user_name, 'Password': password,
                          'SignInURL': f'https://{account}.signin.aws.amazon.com/console',
                          'PasswordResetRequired': True}, exclusive=True)
        iam.create_login_profile(UserName=user_name, Password=password, PasswordResetRequired=True)
    bucket = f'timex-runtime-{account}-{REGION}'
    s3 = session.client('s3')
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
        require_owned(s3.get_bucket_tagging(Bucket=bucket, ExpectedBucketOwner=account)['TagSet'])
    except ClientError as error:
        if error.response['Error']['Code'] not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_tagging(Bucket=bucket, Tagging={'TagSet': tags}, ExpectedBucketOwner=account)
        s3.put_public_access_block(Bucket=bucket, ExpectedBucketOwner=account,
                                  PublicAccessBlockConfiguration={key: True for key in (
                                      'BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')})
        s3.put_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=account, ServerSideEncryptionConfiguration={
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]})
    state.update(account=account, region=REGION, bootstrap_identity=identity['Arn'],
                 deployer_user_arn=user['Arn'], execution_role_arn=role['Arn'], bucket=bucket,
                 updated_at=datetime.now(timezone.utc).isoformat())
    state.setdefault('phase', 'bootstrap_complete')
    save(STATE, state)
    save(ROOT / 'outputs/aws-runtime-deployer-policy.json', deployer)
    save(ROOT / 'outputs/aws-runtime-execution-policy.json', execution)
    print(json.dumps(state, indent=2))
    print('Initial IAM console login details (mode 600):', login_file)


def load_state(account):
    state = json.loads(STATE.read_text())
    if state['account'] != account or state['region'] != REGION:
        raise ValueError('State does not match target account/region')
    return state


def deploy(session, identity, account, *, allow_root_bootstrap=False):
    state = load_state(account)
    explicitly_authorized_root = allow_root_bootstrap and identity['Arn'] == f'arn:aws:iam::{account}:root'
    if identity['Arn'] != state['deployer_user_arn'] and not explicitly_authorized_root:
        raise ValueError('Deployment requires the project IAM identity, not root')
    plan = deployment_plan(ARCHIVE, account_id=account)
    import zipfile
    with zipfile.ZipFile(ARCHIVE) as archive:
        if any(archive.read(name) != (ROOT / name).read_bytes() for name in APPLICATION_FILES):
            raise ValueError('Archive is stale; rebuild before uploading')
    control = session.client('bedrock-agentcore-control')
    runtimes = []
    for page in control.get_paginator('list_agent_runtimes').paginate():
        runtimes.extend(item for item in page['agentRuntimes'] if item['agentRuntimeName'] == 'timex')
    if runtimes:
        raise ValueError('Runtime named timex already exists; use status and review before an update')
    parameters = plan['create_agent_runtime_request_template']
    # Deploy fake first; there is deliberately no cloud copy of the DeepSeek key.
    parameters['environmentVariables'] = {'TIMEX_BIND_HOST': '0.0.0.0', 'TIMEX_ALLOW_LIVE': '0',
                                         'MODEL_PROVIDER': 'fake', 'MAX_MODEL_CALLS': '10',
                                         'MAX_RETRIES': '1', 'MAX_ESTIMATED_USD_PER_RUN': '0.05',
                                         'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}
    parameters['tags'] = TAGS
    parameters['description'] = 'TimeX two-round council; fake-only cloud protocol validation'
    parameters['clientToken'] = str(uuid.uuid5(uuid.NAMESPACE_URL, account + plan['artifact']['sha256']))
    code = parameters['agentRuntimeArtifact']['codeConfiguration']['code']['s3']
    session.client('s3').upload_file(str(ARCHIVE), code['bucket'], code['prefix'],
                                     ExtraArgs={'ExpectedBucketOwner': account, 'ServerSideEncryption': 'AES256'})
    response = control.create_agent_runtime(**parameters)
    state.update({key: response[key] for key in ('agentRuntimeArn', 'agentRuntimeId', 'agentRuntimeVersion', 'status')})
    state.update(phase='runtime_created', artifact=plan['artifact'], s3_object=code['prefix'],
                 deployment_identity=identity['Arn'], updated_at=datetime.now(timezone.utc).isoformat())
    save(STATE, state)
    save(ROOT / 'outputs/aws-runtime-create-request.json', parameters)
    print(json.dumps(state, indent=2))


def status(session, account):
    state = load_state(account)
    response = session.client('bedrock-agentcore-control').get_agent_runtime(agentRuntimeId=state['agentRuntimeId'])
    save(ROOT / 'outputs/aws-runtime-status.json', response)
    state.update(status=response['status'], agentRuntimeVersion=response['agentRuntimeVersion'],
                 phase='runtime_' + response['status'].lower(), updated_at=datetime.now(timezone.utc).isoformat())
    save(STATE, state)
    print(json.dumps({key: response[key] for key in ('agentRuntimeId', 'agentRuntimeVersion', 'status', 'failureReason')
                      if key in response}, indent=2))


def smoke(session, account):
    state = load_state(account)
    client = session.client('bedrock-agentcore', config=Config(read_timeout=180, retries={'total_max_attempts': 1}))
    session_id = str(uuid.uuid4())
    request = {'request': json.loads((ROOT / 'examples/demo_request.json').read_text())}
    try:
        response = client.invoke_agent_runtime(agentRuntimeArn=state['agentRuntimeArn'], runtimeSessionId=session_id,
                                               qualifier='DEFAULT', contentType='application/json', accept='application/json',
                                               payload=json.dumps(request).encode())
        body = json.loads(response['response'].read())
        save(ROOT / 'outputs/aws-runtime-fake-result.json', body)
        assert response['statusCode'] == 200
        assert body['status'] == 'complete' and body['usage']['provider'] == 'fake'
        assert body['usage']['model_calls'] == 9 and body['usage']['estimated_usd'] == 0
        summary = {'status': 'passed', 'http_status': response['statusCode'], 'provider': 'fake',
                   'rule_calls': body['usage']['model_calls'], 'paid_model_calls': 0,
                   'agentRuntimeArn': state['agentRuntimeArn'], 'runtimeSessionId': session_id,
                   'aws_request_id': response['ResponseMetadata']['RequestId'],
                   'tested_at': datetime.now(timezone.utc).isoformat()}
    finally:
        client.stop_runtime_session(agentRuntimeArn=state['agentRuntimeArn'], runtimeSessionId=session_id, qualifier='DEFAULT')
    summary['session_stop_requested'] = True
    save(ROOT / 'outputs/aws-runtime-fake-smoke.json', summary)
    print(json.dumps(summary, indent=2))


def log_status(session, account):
    state = load_state(account)
    client = session.client('logs')
    prefix = '/aws/bedrock-agentcore/runtimes/' + state['agentRuntimeId']
    groups = []
    for page in client.get_paginator('describe_log_groups').paginate(logGroupNamePrefix=prefix):
        groups.extend(page['logGroups'])
    evidence = [{key: group[key] for key in ('logGroupName', 'creationTime', 'retentionInDays') if key in group}
                for group in groups]
    save(ROOT / 'outputs/aws-runtime-log-metadata.json', evidence)
    print(json.dumps({'matching_log_groups': len(groups), 'groups': evidence,
                      'log_contents_read': False, 'retention_modified': False}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'deploy', 'status', 'smoke', 'log-status'])
    parser.add_argument('--profile', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--allow-root-bootstrap', action='store_true',
                        help='Use only for an explicitly authorized one-time root temporary session deployment')
    args = parser.parse_args()
    # Deliberately bypass local proxy configuration for all AWS requests.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    session, identity = session_for(args.profile, args.account_id)
    if args.command == 'bootstrap':
        bootstrap(session, identity, args.account_id)
    elif args.command == 'deploy':
        deploy(session, identity, args.account_id, allow_root_bootstrap=args.allow_root_bootstrap)
    elif args.command == 'status':
        status(session, args.account_id)
    elif args.command == 'smoke':
        smoke(session, args.account_id)
    else:
        log_status(session, args.account_id)


if __name__ == '__main__':
    main()
