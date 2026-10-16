import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('agentcore_live_demo', Path('scripts/agentcore_live_demo.py'))
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize('status, calls, budget', [('partial', 1, 0.001), ('complete', 11, 0.01),
                                                ('complete', 9, 0.051)])
def test_incomplete_or_over_budget_usage_stops_additional_paid_phases(status, calls, budget):
    with pytest.raises(ValueError, match='no further paid phases'):
        demo.check_usage({'status': status, 'usage': {'provider': 'openai', 'model_calls': calls,
                                                   'budget_estimated_usd': budget}})


def test_plan_is_three_councils_with_explicit_total_budget():
    plan = demo.plan()
    assert plan['expected_model_calls'] == 27 and plan['maximum_model_calls'] == 30
    assert plan['total_soft_budget_usd'] == pytest.approx(3 * plan['soft_budget_usd_per_council'])
    assert plan['request']['available_minutes'] is None and plan['request']['requested_minutes'] == 90
    assert '合成' in plan['request']['question']
    memory = demo.plan(memory_only=True)
    assert memory['phases'] == ['memory-default-30', 'current-request-60']
    assert memory['maximum_model_calls'] == 20 and memory['total_soft_budget_usd'] == 0.1
    override = demo.plan(override_only=True, soft_budget_usd=0.035)
    assert override['phases'] == ['current-request-60'] and override['maximum_model_calls'] == 10
    assert override['output_mode'] == 'json_object' and override['total_soft_budget_usd'] == 0.035
    with pytest.raises(ValueError):
        demo.plan(memory_only=True, override_only=True)
    assert demo.plan(memory_only=True, soft_budget_usd=0.035)['total_soft_budget_usd'] == 0.07
    with pytest.raises(ValueError):
        demo.check_usage({'status':'complete','usage':{'provider':'openai','model_calls':9,'budget_estimated_usd':0.04}}, 0.035)


@pytest.mark.parametrize('budget', [0, -0.01, 0.051, float('nan'), float('inf')])
def test_plan_cannot_raise_or_disable_budget(budget):
    with pytest.raises(ValueError):
        demo.plan(soft_budget_usd=budget)


@pytest.mark.parametrize('override_only', [False, True])
def test_partial_first_council_restores_fake_and_stops_without_more_model_calls(monkeypatch, tmp_path, override_only):
    account = '123456789012'
    runtime = {'account': account, 'region': 'us-east-1', 'agentRuntimeId': 'synthetic', 'agentRuntimeArn': 'synthetic-arn'}
    integrations = {'account': account, 'region': 'us-east-1', 'deepseek_secret_arn': 'secret',
                    'gateway_url': 'gateway', 'memory_id': 'synthetic-memory'}
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    for name, value in [('aws-runtime-state.json', runtime), ('aws-integrations-state.json', integrations)]:
        (state_dir / name).write_text(json.dumps(value))
    env = {'TIMEX_ALLOW_LIVE': '0', 'MODEL_PROVIDER': 'fake', 'TIMEX_DEEPSEEK_SECRET_ARN': 'secret',
           'TIMEX_GATEWAY_URL': 'gateway', 'TIMEX_MEMORY_ID': 'synthetic-memory',
           'TIMEX_MEMORY_ACTOR_ID': 'timex-synthetic-demo'}
    current = runtime | {'environmentVariables': env, 'agentRuntimeArtifact': {}, 'roleArn': 'synthetic-role',
                         'networkConfiguration': {}, 'protocolConfiguration': {}, 'lifecycleConfiguration': {}}
    control = SimpleNamespace(get_agent_runtime=lambda **kwargs: current,
                              list_tags_for_resource=lambda **kwargs: {'tags': {'Project':'TimeX','ManagedBy':'timex-runtime-local'}})
    calls, stopped, published = [], [], []

    class Data:
        events = []

        def list_events(self, **kwargs):
            return {'events': self.events}

        def delete_event(self, **kwargs):
            assert kwargs['eventId'] == 'synthetic-event'
            self.events = []

        def invoke_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            payload = json.loads(kwargs['payload'])
            if payload.get('operation') == 'confirm_preference':
                assert payload['confirmed'] is True and payload['preference']['maximum_minutes'] == 30
                self.events = [{'eventId':'synthetic-event','payload':[{'conversational':{
                    'content':{'text':payload['preference']['text']}}}]}]
                result = {'status':'complete','memory':{'event_id':'synthetic-event'}}
                return {'statusCode':200,'ResponseMetadata':{'RequestId':'synthetic'},
                        'response':SimpleNamespace(read=lambda:json.dumps(result).encode())}
            if override_only:
                assert self.events and payload['request']['available_minutes'] == 60
                assert payload['use_memory'] is True and payload['summary_source'] == 'gateway'
            result = {'question': 'Synthetic partial council', 'status': 'partial', 'usage': {'provider': 'openai', 'model_calls': 1,
                      'budget_estimated_usd': 0.001, 'estimated_usd': 0.001},
                      'round1': {}, 'round2': {}, 'proposed_plan': None, 'context_sources': {}}
            return {'statusCode': 200, 'ResponseMetadata': {'RequestId': 'synthetic'},
                    'response': SimpleNamespace(read=lambda: json.dumps(result).encode())}

        def stop_runtime_session(self, **kwargs):
            stopped.append(kwargs)

    def publish(client, current, value):
        published.append(value.copy())
        return current | {'environmentVariables': value, 'agentRuntimeVersion': str(len(published)), 'status': 'READY'}

    monkeypatch.setattr(demo, 'publish', publish)
    session = SimpleNamespace(client=lambda name, **kwargs: control if name == 'bedrock-agentcore-control' else Data())
    result = demo.run(session, account, state_dir, tmp_path / 'report', override_only=override_only)
    assert len(calls) == len(stopped) == (2 if override_only else 1)
    assert published[0]['TIMEX_ALLOW_LIVE'] == '1' and published[-1] == env
    assert result['fake_restored'] is True and result['status'] == 'failed_or_partial'
    assert result['known_model_calls'] == 1 and result['measurement_complete'] is False
    assert result['demo_event_deleted'] is True
    phase = 'current-request-60' if override_only else 'without-preference'
    readable = (tmp_path / 'report' / (phase + '.html')).read_text()
    assert 'partial' in readable and 'Synthetic partial council' in readable
    assert json.loads((state_dir / 'aws-runtime-state.json').read_text())['agentRuntimeVersion'] == '2'
