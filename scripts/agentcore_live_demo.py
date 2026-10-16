"""Three explicitly authorized cloud councils; restore fake mode and remove demo memory."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_cloud import ROOT, REGION, require_owned, save, session_for
from integrations_cloud import wait_ready
from collect_article_materials import html, render_markdown
from timex.cli import markdown
from timex.models import CouncilResult, DeliberationRequest

PHASES = ('without-preference', 'memory-default-30', 'current-request-60')
LIVE_ENV = {
    'TIMEX_ALLOW_LIVE': '1', 'MODEL_PROVIDER': 'openai', 'MODEL_NAME': 'deepseek-flash',
    'OPENAI_BASE_URL': 'https://api.deepseek.com', 'MODEL_THINKING': 'disabled',
    'MODEL_INPUT_USD_PER_MILLION': '0.30', 'MODEL_OUTPUT_USD_PER_MILLION': '1.20',
    'MAX_MODEL_CALLS': '10', 'MAX_RETRIES': '1', 'MAX_ESTIMATED_USD_PER_RUN': '0.05',
    'MAX_INPUT_TOKENS': '24000', 'TOTAL_TIMEOUT_SECONDS': '180',
    'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false',
}


def plan():
    return {'scenario': 'synthetic interest exploration, Gateway facts and confirmed Memory',
            'phases': list(PHASES), 'expected_model_calls': 27, 'maximum_model_calls': 30,
            'soft_budget_usd_per_council': 0.05, 'total_soft_budget_usd': 0.15,
            'model': 'deepseek-flash', 'thinking': 'disabled', 'region': REGION,
            'pricing_basis': 'peak cache-miss $0.30 input / $1.20 output per million tokens',
            'pricing_source': 'https://api-docs.deepseek.com/quick_start/pricing/',
            'pricing_checked_date': '2026-10-16', 'aws_cost': 'not measured; charged separately',
            'cleanup': ['stop each Runtime session', 'restore original fake configuration',
                        'delete only the demo-created Memory event'],
            'request': json.loads((ROOT / 'examples/agentcore_live_request.json').read_text())}


def publish(control, current, environment):
    parameters = {key: copy.deepcopy(current[key]) for key in (
        'agentRuntimeArtifact', 'roleArn', 'networkConfiguration', 'protocolConfiguration', 'lifecycleConfiguration')}
    parameters.update(agentRuntimeId=current['agentRuntimeId'], environmentVariables=environment)
    updated = control.update_agent_runtime(**parameters)
    ready = wait_ready(lambda: control.get_agent_runtime(agentRuntimeId=current['agentRuntimeId']))
    endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=current['agentRuntimeId'], endpointName='DEFAULT')
    if endpoint['liveVersion'] != updated['agentRuntimeVersion']:
        control.update_agent_runtime_endpoint(agentRuntimeId=current['agentRuntimeId'], endpointName='DEFAULT',
                                               agentRuntimeVersion=updated['agentRuntimeVersion'])
        wait_ready(lambda: control.get_agent_runtime_endpoint(agentRuntimeId=current['agentRuntimeId'], endpointName='DEFAULT'))
    return ready


def check_usage(result):
    usage = result['usage']
    if (result['status'] != 'complete' or usage['provider'] != 'openai'
            or not 9 <= usage['model_calls'] <= 10
            or usage['budget_estimated_usd'] > 0.05):
        raise ValueError('Incomplete or over-budget council; no further paid phases will run')


def run(session, account, state_dir, output):
    from botocore.config import Config
    if (output / 'run-started.json').exists():
        raise ValueError('A run already started here; choose a new directory rather than replaying paid calls')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(output / 'plan.json', plan())
    original = json.loads((state_dir / 'aws-runtime-state.json').read_text())
    integrations = json.loads((state_dir / 'aws-integrations-state.json').read_text())
    if any(state['account'] != account or state['region'] != REGION for state in (original, integrations)):
        raise ValueError('State belongs to a different account/region')
    control = session.client('bedrock-agentcore-control')
    current = control.get_agent_runtime(agentRuntimeId=original['agentRuntimeId'])
    require_owned(control.list_tags_for_resource(resourceArn=current['agentRuntimeArn'])['tags'])
    env = current['environmentVariables']
    if (env.get('TIMEX_ALLOW_LIVE') != '0' or env.get('MODEL_PROVIDER') != 'fake'
            or 'OPENAI_API_KEY' in env or 'DEEPSEEK_API_KEY' in env
            or env.get('TIMEX_DEEPSEEK_SECRET_ARN') != integrations['deepseek_secret_arn']
            or env.get('TIMEX_GATEWAY_URL') != integrations['gateway_url']
            or env.get('TIMEX_MEMORY_ID') != integrations['memory_id']
            or env.get('TIMEX_MEMORY_ACTOR_ID') != 'timex-synthetic-demo'):
        raise ValueError('Expected the owned fake Runtime with server-selected Gateway/Memory/Secret')
    data = session.client('bedrock-agentcore', config=Config(connect_timeout=5, read_timeout=240,
                                                           retries={'total_max_attempts': 1}))
    scope = {'memoryId': integrations['memory_id'], 'actorId': env['TIMEX_MEMORY_ACTOR_ID'],
             'sessionId': 'confirmed-preferences'}
    page = data.list_events(**scope, includePayloads=False, maxResults=100)
    if page['events'] or page.get('nextToken'):
        raise ValueError('Refusing to overwrite existing preferences in the synthetic stream')
    nonce = str(uuid.uuid4())
    report = {'status': 'started', 'created_at': datetime.now(timezone.utc).isoformat(), 'synthetic_only': True,
              'runtime_id': original['agentRuntimeId'], 'phases': [], 'operations': [], 'sessions': [],
              'fake_restored': False, 'demo_event_deleted': False,
              'aws_cost': 'not measured; model estimates are not an AWS bill',
              'code_commit': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True,
                                            capture_output=True, text=True).stdout.strip()}
    save(output / 'run-started.json', {'run_id': nonce, 'started_at': report['created_at']})
    restore = {key: copy.deepcopy(current[key]) for key in (
        'agentRuntimeId', 'agentRuntimeArtifact', 'roleArn', 'networkConfiguration',
        'protocolConfiguration', 'lifecycleConfiguration', 'environmentVariables')}
    save(output / 'restore-request.json', restore)
    created_event = None

    def invoke(label, payload):
        sid = str(uuid.uuid4())
        started = time.monotonic()
        meta = {'label': label, 'session_id': sid, 'stop_requested': False}
        report['sessions'].append(meta)
        save(output / (label + '-payload.json'), payload)
        try:
            response = data.invoke_agent_runtime(agentRuntimeArn=original['agentRuntimeArn'],
                runtimeSessionId=sid, qualifier='DEFAULT', contentType='application/json',
                accept='application/json', payload=json.dumps(payload).encode())
            meta.update(aws_request_id=response['ResponseMetadata']['RequestId'], http_status=response['statusCode'])
            result = json.loads(response['response'].read())
            save(output / (label + '-result.json'), result)
            if response['statusCode'] != 200:
                raise ValueError('Runtime returned a non-success response')
            return result
        finally:
            meta['wall_seconds'] = round(time.monotonic() - started, 3)
            try:
                data.stop_runtime_session(agentRuntimeArn=original['agentRuntimeArn'], runtimeSessionId=sid, qualifier='DEFAULT')
                meta['stop_requested'] = True
            finally:
                save(output / 'summary.json', report)

    try:
        ready = publish(control, current, dict(env) | LIVE_ENV)
        report['live_runtime_version'] = ready['agentRuntimeVersion']
        request = plan()['request']
        for phase in PHASES:
            value = copy.deepcopy(request)
            if phase == 'memory-default-30':
                written = invoke('confirm-preference', {'operation': 'confirm_preference', 'confirmed': True,
                    'preference': {'text': f'【合成确认偏好 {nonce}】后续兴趣小实验默认新增投入最多30分钟，当前明确要求可覆盖。',
                                   'maximum_minutes': 30}})
                created_event = written['memory']['event_id']
                report['operations'].append({'operation': 'confirm_preference', 'event_id': created_event})
            elif phase == 'current-request-60':
                value['available_minutes'] = 60
            result = invoke(phase, {'request': value, 'summary_source': 'gateway', 'use_memory': True, 'live': True})
            report['phases'].append({'phase': phase, 'usage': result['usage'],
                'round1_minutes': {r: p['suggested_minutes'] for r, p in result['round1'].items()},
                'round2_minutes': {r: p['suggested_minutes'] for r, p in result['round2'].items()},
                'plan': result['proposed_plan'], 'context_sources': result['context_sources']})
            save(output / 'summary.json', report)
            check_usage(result)
            memory = result['context_sources']['memory']
            if phase == 'without-preference':
                assert memory['event_id'] is None
            elif phase == 'memory-default-30':
                assert memory['event_id'] == created_event and memory['applied'] is True
            else:
                assert memory['precedence'] == 'current_request' and memory['conflict'] is True
            council = CouncilResult.model_validate({k: v for k, v in result.items() if k != 'context_sources'})
            save(output / (phase + '-context.json'), result['context_sources'])
            (output / (phase + '.html')).write_text(html(phase,
                '<p>实际 AgentCore + DeepSeek；完全合成情境，原始发言未经改写。</p>' + render_markdown(markdown(council))))
            print(json.dumps({'phase': phase, 'status': result['status'], 'calls': result['usage']['model_calls'],
                              'estimated_usd': result['usage']['estimated_usd']}, ensure_ascii=False), flush=True)
        assert len({p['context_sources']['time_summary']['snapshot_sha256'] for p in report['phases']}) == 1
        report['status'] = 'complete'
    except Exception as error:
        report.update(status='failed_or_partial', error_type=type(error).__name__)
        # SDK/provider exception messages can include private request bodies.
    finally:
        try:
            for attempt in range(2):
                try:
                    restored = publish(control, current, dict(env))
                    break
                except Exception:
                    if attempt == 1:
                        raise
            restored_env = restored['environmentVariables']
            assert restored_env == env and restored_env['TIMEX_ALLOW_LIVE'] == '0'
            report.update(fake_restored=True, restored_runtime_version=restored['agentRuntimeVersion'])
            original.update(agentRuntimeVersion=restored['agentRuntimeVersion'], status=restored['status'],
                            phase='runtime_ready', updated_at=datetime.now(timezone.utc).isoformat())
            integrations.update(runtime_version=restored['agentRuntimeVersion'])
            save(state_dir / 'aws-runtime-state.json', original)
            save(state_dir / 'aws-integrations-state.json', integrations)
        except Exception as error:
            report['restore_error_type'] = type(error).__name__
        try:
            # Initial stream was empty. Delete only events carrying this run's nonce.
            page = data.list_events(**scope, includePayloads=True, maxResults=100)
            removed = []
            for event in page['events']:
                if any(nonce in p.get('conversational', {}).get('content', {}).get('text', '')
                       for p in event.get('payload', [])):
                    data.delete_event(**scope, eventId=event['eventId'])
                    removed.append(event['eventId'])
            report['deleted_event_ids'] = removed
            report['demo_event_deleted'] = created_event is None or created_event in removed
            report['remaining_event_count'] = len(data.list_events(**scope, includePayloads=False, maxResults=100)['events'])
        except Exception as error:
            report['memory_cleanup_error_type'] = type(error).__name__
        report['known_model_calls'] = sum(p['usage']['model_calls'] for p in report['phases'])
        costs = [p['usage']['estimated_usd'] for p in report['phases']]
        report['known_model_estimated_usd'] = sum(c for c in costs if c is not None)
        report['measurement_complete'] = report['status'] == 'complete' and all(c is not None for c in costs)
        save(output / 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'run'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'outputs')
    parser.add_argument('--profile')
    parser.add_argument('--account-id')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.command == 'plan':
        save(args.output / 'plan.json', plan(), exclusive=True)
        print(args.output / 'plan.json')
    else:
        if not args.live or not args.profile or not args.account_id:
            parser.error('run requires --live, --profile and --account-id')
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(name, None)
        session, identity = session_for(args.profile, args.account_id)
        result = run(session, args.account_id, args.state_dir, args.output)
        print(json.dumps({k: result.get(k) for k in ('status', 'known_model_calls', 'known_model_estimated_usd',
                                                   'fake_restored', 'demo_event_deleted')}, indent=2))
        raise SystemExit(0 if result['status'] == 'complete' and result['fake_restored'] and result['demo_event_deleted'] else 1)
