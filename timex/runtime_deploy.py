"""Local-only CodeZip packaging and deployment review; no AWS service calls."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


APPLICATION_FILES = (
    'runtime_entrypoint.py', 'requirements-runtime.lock',
    *('timex/' + name + '.py' for name in (
        '__init__', 'config', 'models', 'roles', 'validation', 'providers', 'calling', 'graph', 'runtime', 'gateway', 'memory')),
)


def _check_member(name, header=b''):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts
            or any(part in {'.git', '.aws', '.venv'} or part.startswith('.env')
                   for part in path.parts)
            or path.parts[0] == 'outputs' or name.startswith('data/private/')
            or path.suffix in {'.key', '.pyd', '.dll', '.dylib'}):
        raise ValueError('Unsafe package member: ' + name)
    if name.startswith('timex/') and name not in APPLICATION_FILES:
        raise ValueError('Unexpected application file: ' + name)
    if path.suffix == '.so':
        if (len(header) < 20 or header[:4] != b'\x7fELF' or header[5] != 1
                or int.from_bytes(header[18:20], 'little') != 183):
            raise ValueError('Native library must be Linux ARM64: ' + name)
        if 'cpython-' in name and 'cpython-312-' not in name:
            raise ValueError('Native library must target Python 3.12: ' + name)


def inspect_package(archive):
    native_libraries = 0
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or not set(APPLICATION_FILES) <= set(names):
            raise ValueError('Package has duplicate or missing application files')
        total = sum(item.file_size for item in package.infolist())
        if total > 750 * 1024 * 1024 or Path(archive).stat().st_size > 250 * 1024 * 1024:
            raise ValueError('Package exceeds AgentCore CodeZip size limits')
        for item in package.infolist():
            with package.open(item) as source:
                _check_member(item.filename, source.read(20))
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Package must not contain symbolic links')
            native_libraries += item.filename.endswith('.so')
    return {'runtime': 'PYTHON_3_12', 'platform': 'Linux ARM64', 'files': len(names),
            'native_libraries_checked': native_libraries, 'uncompressed_bytes': total,
            'zip_bytes': Path(archive).stat().st_size,
            'sha256': hashlib.sha256(Path(archive).read_bytes()).hexdigest()}


def build_package(source_root, dependencies, output):
    source_root, dependencies, output = map(Path, (source_root, dependencies, output))
    if output.exists() or output.resolve().is_relative_to(dependencies.resolve()):
        raise ValueError('Choose a new output outside the dependency directory')
    if not (dependencies / 'bedrock_agentcore/runtime/app.py').is_file():
        raise ValueError('Install locked Linux ARM64 dependencies into a separate directory first')
    files = {name: source_root / name for name in APPLICATION_FILES}
    for name, source in files.items():
        if source.is_symlink() or not source.resolve().is_relative_to(source_root.resolve()):
            raise ValueError('Application source must not be a symbolic link')
        if not source.is_file():
            raise ValueError('Missing application file: ' + name)
    for source in dependencies.rglob('*'):
        if source.is_symlink():
            raise ValueError('Dependency directory must not contain symbolic links')
        if not source.is_file():
            continue
        relative = source.relative_to(dependencies)
        if '__pycache__' in relative.parts or relative.parts[0] in {'bin', 'Scripts'} or source.suffix == '.pyc':
            continue
        name = relative.as_posix()
        if name in files:
            raise ValueError('Dependency shadows application source: ' + name)
        with source.open('rb') as stream:
            _check_member(name, stream.read(20))
        files[name] = source
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x') as package:
        for name, source in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, source.read_bytes())
    return inspect_package(output)


def deployment_plan(archive, account_id=None, region='us-east-1', profile='timex-deployer'):
    if account_id is not None and not re.fullmatch(r'\d{12}', account_id):
        raise ValueError('AWS account ID must contain exactly 12 digits')
    if region != 'us-east-1':
        raise ValueError('This deployment has only been prepared for us-east-1')
    artifact = inspect_package(archive)
    account = account_id or '${ACCOUNT_ID}'
    bucket = f'timex-runtime-{account}-{region}'
    object_key = 'timex/' + artifact['sha256'] + '.zip'
    role = f'arn:aws:iam::{account}:role/timex-runtime'
    runtime_arn = f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/*'
    secret = f'arn:aws:secretsmanager:{region}:{account}:secret:timex/deepseek-*'
    logs = f'arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*'
    parameters = {
        'agentRuntimeName': 'timex', 'roleArn': role,
        'agentRuntimeArtifact': {'codeConfiguration': {
            'code': {'s3': {'bucket': bucket, 'prefix': object_key}},
            'runtime': 'PYTHON_3_12', 'entryPoint': ['runtime_entrypoint.py']}},
        'networkConfiguration': {'networkMode': 'PUBLIC'},
        'protocolConfiguration': {'serverProtocol': 'HTTP'},
        'lifecycleConfiguration': {'idleRuntimeSessionTimeout': 60, 'maxLifetime': 600},
        'environmentVariables': {
            'TIMEX_BIND_HOST': '0.0.0.0', 'TIMEX_ALLOW_LIVE': '0',
            'TIMEX_DEEPSEEK_SECRET_ARN': '${DEEPSEEK_SECRET_ARN}',
            'MODEL_PROVIDER': 'openai', 'MODEL_NAME': 'deepseek-flash',
            'OPENAI_BASE_URL': 'https://api.deepseek.com', 'MODEL_THINKING': 'disabled',
            'MODEL_INPUT_USD_PER_MILLION': '0.30', 'MODEL_OUTPUT_USD_PER_MILLION': '1.20',
            'MAX_MODEL_CALLS': '10', 'MAX_RETRIES': '1', 'MAX_ESTIMATED_USD_PER_RUN': '0.05',
            'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false',
        },
    }
    if account_id:
        # Validate against the installed SDK model without constructing a client.
        from botocore.session import Session
        from botocore.validate import validate_parameters
        shape = Session().get_service_model('bedrock-agentcore-control').operation_model('CreateAgentRuntime').input_shape
        validate_parameters(parameters, shape)
    return {
        'status': 'local_package_ready_cloud_preflight_pending', 'aws_calls_made': 0,
        'region': region, 'profile_to_configure': profile, 'artifact': artifact,
        'create_agent_runtime_request_template': parameters,
        'runtime_role_trust_template': {'Version': '2012-10-17', 'Statement': [{
            'Effect': 'Allow', 'Principal': {'Service': 'bedrock-agentcore.amazonaws.com'},
            'Action': 'sts:AssumeRole', 'Condition': {
                'StringEquals': {'aws:SourceAccount': account},
                'ArnLike': {'aws:SourceArn': runtime_arn}}}]},
        'runtime_role_permissions_draft': {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': f'arn:aws:s3:::{bucket}/{object_key}'},
            {'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': secret},
            {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup'], 'Resource': logs},
            {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
             'Resource': logs + ':*'},
        ]},
        'deployer_pass_role_template': {
            'Effect': 'Allow', 'Action': ['iam:PassRole'], 'Resource': role,
            'Condition': {'StringEquals': {'iam:PassedToService': 'bedrock-agentcore.amazonaws.com'}}},
        'pending_before_any_deployment': [
            'Establish a non-root deployment identity and browser-based temporary login; no root access keys.',
            'Verify account ID and project permissions, including create/update/invoke and any dependent actions.',
            'Review role/log delivery permissions, S3 bucket and secret configuration in the selected account.',
            'Set a short CloudWatch log retention and review AWS runtime, S3, logs and secret charges.',
            'First cloud protocol smoke may stay fake; enabling live requires server opt-in and explicit call budget.',
            'Obtain authorization for the concrete resource set and test before upload or resource creation.',
        ],
        'planned_resources': ['one AgentCore Runtime', 'one execution role', 'one artifact bucket/object',
                              'one DeepSeek secret', 'Runtime CloudWatch log group'],
        'cleanup_order': ['delete Runtime', 'delete only its logs', 'remove artifact and empty project bucket',
                          'schedule project secret deletion', 'remove project role policies and execution role'],
        'sources_checked_2026_10_15': [
            'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html',
            'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html',
            'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html',
            'https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--dependencies', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('--archive', type=Path, required=True)
    plan.add_argument('--account-id')
    plan.add_argument('--profile', default='timex-deployer')
    plan.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build_package(Path(__file__).resolve().parents[1], args.dependencies, args.output)
            destination = args.output.with_suffix('.manifest.json')
        else:
            result = deployment_plan(args.archive, account_id=args.account_id, profile=args.profile)
            destination = args.output
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
        print(str(destination.resolve()))
    except (ValueError, OSError, zipfile.BadZipFile):
        parser.exit(2, 'Invalid package, target, or existing output; inspect inputs locally.\n')


if __name__ == '__main__':
    main()
