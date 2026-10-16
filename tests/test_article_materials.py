import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('article_materials', Path('scripts/collect_article_materials.py'))
materials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(materials)


@pytest.mark.parametrize('provider, question', [('openai', 'synthetic'), ('fake', 'private question')])
def test_materials_refuse_live_or_non_demo_council_evidence(provider, question):
    with pytest.raises(ValueError, match='non-demo or live'):
        materials.validate_report('aws-gateway-council.json',
                                  {'usage': {'provider': provider}, 'question': question}, 'synthetic')


def test_materials_require_explicit_synthetic_smoke_declaration():
    with pytest.raises(ValueError, match='synthetic-only'):
        materials.validate_report('aws-memory-smoke.json', {}, 'synthetic')
    materials.validate_report('aws-memory-smoke.json', {'synthetic_only': True}, 'synthetic')


def test_materials_use_explicit_allowlist_without_private_inputs_or_article_drafts():
    for path in (*materials.SOURCES, *materials.REPORTS):
        assert '.env' not in path and 'private/' not in path and 'credentials' not in path
        assert 'first-login' not in path and 'article-draft' not in path


def test_only_exact_public_synthetic_inputs_are_allowed_for_historical_live():
    demo = json.loads(Path('examples/demo_request.json').read_text())
    data = {'request': demo, 'mode': 'live', 'model_settings': {'base_url': 'https://api.deepseek.com'}}
    materials.validate_historical(data, demo)
    data['request'] = demo | {'question': 'Private account question'}
    with pytest.raises(ValueError, match='differs from the public'):
        materials.validate_historical(data, demo)
