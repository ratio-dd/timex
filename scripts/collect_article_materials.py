"""Collect an allowlist of synthetic evidence and source files, without network access."""
import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from timex.cli import comparison_markdown, markdown
from timex.models import CouncilResult, DeliberationRequest
from timex.runtime_deploy import APPLICATION_FILES

REPORTS = (
    'aws-integrations-deployment-report.json',
    'aws-runtime-fake-smoke.json', 'aws-runtime-fake-result.json',
    'aws-gateway-smoke.json', 'aws-gateway-council.json',
    'aws-memory-smoke.json',
    *('aws-memory-' + phase + '-council.json' for phase in (
        'before', 'after-confirm', 'current-override', 'after-correct', 'after-delete')),
    'aws-deepseek-secret-upload.json', 'aws-deepseek-runtime-check.json',
    'aws-gateway-permissions.json', 'aws-memory-permissions.json', 'aws-deepseek-secret-permissions.json',
    'aws-runtime-execution-policy.json', 'aws-runtime-deployer-policy.json',
    'offline-validation.json',
)
HISTORICAL = (
    'outputs/local-deepseek-20261015-215806/comparison.json',
    'outputs/issue8-deepseek-20261015-221235/comparison.json',
)
SOURCES = tuple(dict.fromkeys((
    'README.md', 'pyproject.toml', '.github/workflows/ci.yml',
    'requirements-live.lock', *APPLICATION_FILES,
    'timex/cli.py', 'timex/comparison.py', 'timex/time_data.py', 'timex/gateway_tool.py',
    'timex/runtime_deploy.py', 'timex/atimelogger.py',
    'docs/M1.md', 'docs/POC.md', 'docs/ATIMELOGGER.md',
    'scripts/runtime_cloud.py', 'scripts/integrations_cloud.py', 'scripts/collect_article_materials.py',
    'examples/demo_request.json', 'examples/demo_events.csv', 'examples/m1-comparison-fake.md',
    'tests/test_runtime.py', 'tests/test_runtime_deploy.py', 'tests/test_runtime_cloud.py',
    'tests/test_gateway.py', 'tests/test_memory.py', 'tests/test_cloud_integrations.py', 'tests/test_prompts.py',
    'tests/test_article_materials.py',
)))
STYLE = '''body{font:16px/1.7 system-ui,sans-serif;max-width:1050px;margin:40px auto;padding:0 24px;color:#183040}
h1,h2,h3{line-height:1.3}a{color:#0759a0}pre{white-space:pre-wrap;background:#f3f5f7;padding:18px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd6df;padding:10px;text-align:left}small{color:#536778}'''


def html(title, body):
    return f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(title)}</title><style>{STYLE}</style><body>{body}</body></html>\n'


def validate_report(name, data, question):
    if name.endswith('-council.json') or name == 'aws-runtime-fake-result.json':
        if data.get('usage', {}).get('provider') != 'fake' or data.get('question') != question:
            raise ValueError('Refusing non-demo or live council evidence')
    if name in {'aws-gateway-smoke.json', 'aws-memory-smoke.json'} and data.get('synthetic_only') is not True:
        raise ValueError('Refusing evidence without a synthetic-only declaration')


def validate_historical(data, demo):
    value = DeliberationRequest.model_validate(data['request']).model_dump(mode='json')
    expected = DeliberationRequest.model_validate(demo).model_dump(mode='json')
    if (value != expected or data.get('mode') != 'live'
            or data.get('model_settings', {}).get('base_url') != 'https://api.deepseek.com'):
        raise ValueError('Refusing historical input that differs from the public synthetic demo')


def render_markdown(text):
    lines, code = [], None
    for line in text.splitlines():
        if line.startswith('```'):
            if code is None:
                code = []
            else:
                lines.append('<pre>' + escape('\n'.join(code)) + '</pre>')
                code = None
        elif code is not None:
            code.append(line)
        elif line:
            level = len(line) - len(line.lstrip('#'))
            if 1 <= level <= 6 and line[level:level + 1] == ' ':
                lines.append(f'<h{level}>{escape(line[level + 1:])}</h{level}>')
            else:
                lines.append('<p>' + escape(line) + '</p>')
    return ''.join(lines)


def collect(output):
    if output.exists():
        raise ValueError('Choose a new output directory to avoid mixing snapshots')
    state = json.loads((ROOT / 'outputs/aws-integrations-state.json').read_text())
    replacements = {str(ROOT): '${PROJECT_ROOT}', state['account']: '000000000000'}
    for field, placeholder in (
        ('gateway_id', 'timex-summary-GATEWAY_ID'), ('gateway_target_id', 'GATEWAY_TARGET_ID'),
        ('memory_id', 'timex_preferences-MEMORY_ID'), ('deepseek_secret_arn',
         'arn:aws:secretsmanager:us-east-1:000000000000:secret:timex/deepseek-REDACTED'),
        ('deepseek_secret_version', 'SECRET_VERSION_ID')):
        if state.get(field):
            replacements[state[field]] = placeholder
    runtime = json.loads((ROOT / 'outputs/aws-runtime-state.json').read_text())
    replacements[runtime['agentRuntimeId']] = 'timex-RUNTIME_ID'

    def redact(text):
        for original, replacement in sorted(replacements.items(), key=lambda item: -len(item[0])):
            text = text.replace(original, replacement)
        return text

    demo = json.loads((ROOT / 'examples/demo_request.json').read_text())
    question = demo['question']
    files, manifest, reports = {}, [], {}
    for name in REPORTS:
        source = ROOT / 'outputs' / name
        if not source.is_file():
            raise ValueError('Missing expected evidence: ' + name)
        data = json.loads(source.read_text())
        validate_report(name, data, question)
        reports[name] = data
        files['evidence/' + name] = redact(source.read_text())
        manifest.append({'source': source.relative_to(ROOT).as_posix(), 'export': 'evidence/' + name,
                         'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'identifiers_redacted': True})
    for name in SOURCES:
        source = ROOT / name
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError('Source must remain within the repository')
        files['code/' + name] = redact(source.read_text())
        manifest.append({'source': name, 'export': 'code/' + name,
                         'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    validation = ROOT / 'outputs/offline-tests.xml'
    files['evidence/offline-tests.xml'] = redact(validation.read_text())
    manifest.append({'source': 'outputs/offline-tests.xml', 'export': 'evidence/offline-tests.xml',
                     'source_sha256': hashlib.sha256(validation.read_bytes()).hexdigest()})

    historical_links = []
    for name in HISTORICAL:
        source = ROOT / name
        data = json.loads(source.read_text())
        validate_historical(data, demo)
        destination = 'historical-live/' + source.parent.name
        for original in (source, source.with_suffix('.md')):
            target = destination + '/' + original.name
            files[target] = redact(original.read_text())
            manifest.append({'source': original.relative_to(ROOT).as_posix(), 'export': target,
                             'source_sha256': hashlib.sha256(original.read_bytes()).hexdigest()})
        path = 'readable/' + source.parent.name + '.html'
        files[path] = redact(html(source.parent.name, '<p>历史本地 DeepSeek 真实模型报告；输入经规范化后与公开合成示例完全相同。不是 AWS 上的 DeepSeek 实测，本轮整理未重跑。</p>'
                                 + render_markdown(comparison_markdown(data))))
        historical_links.append(f'<li><a href="{path}">{source.parent.name}：完整真实模型对照</a>（估算 ${data["total_usage"]["estimated_usd"]:.7f}，不是账单）</li>')
    # These companions belong only to the already validated synthetic comparison.
    for name in ('semantic-review.json', 'run-manifest.json', 'prompt-changes.patch'):
        source = ROOT / 'outputs/issue8-deepseek-20261015-221235' / name
        destination = 'historical-live/issue8-deepseek-20261015-221235/' + name
        files[destination] = redact(source.read_text())
        manifest.append({'source': source.relative_to(ROOT).as_posix(), 'export': destination,
                         'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})

    readable = []
    for name, data in reports.items():
        if name.endswith('-council.json') or name == 'aws-runtime-fake-result.json':
            council = CouncilResult.model_validate({key: value for key, value in data.items()
                                                     if key != 'context_sources'})
            context = escape(json.dumps(data.get('context_sources', {}), ensure_ascii=False, indent=2))
            path = 'readable/' + Path(name).stem + '.html'
            files[path] = redact(html(name, '<p>云服务真实执行，发言由 fake 规则生成；不是 DeepSeek 对白。仅使用合成输入，未人工改写发言。</p>'
                                    + render_markdown(markdown(council)) + '<h2>共同事实与记忆来源</h2><pre>' + context + '</pre>'))
            readable.append(f'<li><a href="{path}">{escape(name)}</a></li>')

    phases = reports['aws-memory-smoke.json']['phases']
    table = ''.join('<tr><td>' + escape(p['phase']) + '</td><td>'
                    + escape(json.dumps(p['round1_minutes'], ensure_ascii=False)) + '</td><td>'
                    + escape(p['memory']['precedence']) + '</td></tr>' for p in phases)
    checks = reports['aws-deepseek-runtime-check.json']
    notes = {
        'scope': '2026-10-15 AWS 合成 fake 部署与测试，加两份已存在的本地 DeepSeek 合成实测；本轮整理/上传 Key 未新增模型调用，文章正文未修改。',
        'claim_boundaries': [
            '实际云端 Runtime/Gateway/Memory 已通；fake 规则发言不能充当 DeepSeek 协商质量证据。',
            'Gateway 数据来自随 Lambda 打包的合成 CSV；未接入私人 aTimeLogger 账户或外部 MCP 服务。',
            'Memory 为 7 天短期确认事件；没有长期自动提取、后台提取角色或向量数据库。',
            'Memory 的五次会议有不同 Runtime 会话 ID；事件 ID 对应 AWS 写入结果，共同摘要 SHA 一致。',
            'actor 固定在可信服务端配置，尚缺经验证的调用者到用户身份映射；不能宣称多用户隔离完成。',
            '当前请求覆盖仅验证结构化 available_minutes；自然语言约束冲突解析未实现。',
            'Key 已存 Secrets Manager；只做上传身份的存取校验，未验证 Runtime 执行角色取值或 DeepSeek 有效性。',
            '缺少 PR review、用户体验评价及实际 AWS 费用，不能仅因测试通过关闭相关 issue。',
        ],
        'observed_failures': [
            {'failure': 'Lambda 创建时执行角色暂不可被服务假定',
             'fix': '仅针对角色传播错误，有界最多 4 次创建尝试，每次间隔 3 秒；离线边界测试通过。'},
            {'failure': '最小 Lambda 包导入 timex 时触发 eager graph 导入，缺少 timex.graph',
             'fix': '__init__.py 改为按需导入 run_council；最小包隔离导入测试和后续真实 Gateway 调用通过。'},
        ],
        'failure_evidence_boundary': '失败描述来自本聊天实际部署过程与修复代码；此资料包没有 CloudWatch 日志正文或失败账单，不能据此编造截图。',
        'cost_and_cleanup': 'AWS fake 阶段及本轮整理新增付费模型调用 0；两份历史本地 DeepSeek 报告各含 10 次模型调用，按报告配置价估算合计 $0.0237486，不是账单。AWS 各服务及失败尝试费用未测量。测试会话已请求停止，合成偏好已删除，云资源和 Secret 保留。日志保留期未修改。',
        'unresolved_semantics': ['不把旧接受条件说成新让步', '不把职业产出作为探索投入的必要门槛', '不把本周投入总额擅自解释为连续投入时长'],
        'redaction': '账号、项目资源标识、Secret ARN/版本和本机项目路径替换为占位符；会话/请求/事件 ID 保留用于证据关联。只采用显式文件白名单。',
        'excluded': ['.env / Key 明文 / AWS 临时凭证 / IAM 初始密码文件', '私人流水与私人真实模型报告', '现有文章草稿和其他 thread 的文章改动', 'build ZIP / .venv / 依赖二进制'],
    }
    files['notes/limits-failures-costs.json'] = json.dumps(notes, ensure_ascii=False, indent=2) + '\n'
    sources = [
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-create-a-memory-store.html',
        'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/using-memory-short-term.html',
        'https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_CreateSecret.html',
        'https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access_iam-policies.html',
    ]
    files['notes/sources.json'] = json.dumps({'checked_date': '2026-10-15', 'official_sources': sources,
        'issues': [f'https://github.com/ratio-dd/timex/issues/{i}' for i in (7, 8, 9, 10, 11, 12, 13, 14)]}, indent=2) + '\n'
    links = ''.join(f'<li><a href="evidence/{name}">{name}</a></li>' for name in REPORTS)
    files['index.html'] = html('TimeX 文章素材索引', f'''<h1>TimeX 文章素材 · 2026-10-15</h1>
<p>这是可引用的工程证据与源码快照，不是文章正文。资料分为历史本地 DeepSeek 真实模型对照和 AWS fake 集成两类，均为合成输入；本轮整理和上传 Key 新增付费模型调用为 0。</p>
<p>最新凭证配置检查：Runtime v{escape(checks['runtime_version'])} / {escape(checks['runtime_status'])}，live 关闭。Gateway/Memory 对照报告属于此前 v3；不要把 v4 配置核验说成重新跑过整组实测。</p>
<h2>建议先读</h2><ol><li><a href="code/README.md">README：运行步骤、权限表、issue 验收差距</a></li>
<li><a href="notes/limits-failures-costs.json">事实边界、实际失败、修复、费用与清理</a></li>
<li>下面的完整可读对话：必须区分历史本地 DeepSeek 和 AWS fake</li>
<li><a href="notes/sources.json">官方依据和相关 issue</a>；<a href="manifest.json">文件来源与原件 SHA-256</a></li></ol>
<p>code/docs 下的 M1、POC 和时间适配说明为历史文档原件，可能包含旧阶段计划；当前 AWS 状态与验收边界以本索引、最新配置报告和 README 为准。文章正文没有收录。</p>
<h2>历史 DeepSeek 对照：为什么四个角色都暂缓</h2><ul>{''.join(historical_links)}</ul>
<p>两份报告使用相同合成事实、模型配置和预算，角色与提示词同时变化；只有单次前后观察，不能证明普遍质量提升。探索角色在修改后第一轮建议 60 分钟，第二轮仍回到 0，其他角色仍暂缓。</p>
<p><a href="historical-live/issue8-deepseek-20261015-221235/semantic-review.json">助手复盘与三项未解决语义问题</a>；用户评分未提供，不能当作用户验收。不要把历史本地模型费用加在 AWS fake 阶段上。</p>
<h2>Memory 前后对照</h2><p>每一行对应独立 Runtime 会话，摘要快照一致。分钟数为第一轮角色建议，不能直接当作用户最终选择。</p>
<table><tr><th>阶段</th><th>四角色建议分钟</th><th>采用来源</th></tr>{table}</table>
<h2>AWS fake 完整对话</h2><ul>{''.join(readable)}</ul>
<h2>原始结构化证据（已脱敏）</h2><ul>{links}</ul>
<h2>关键代码路径</h2><ul><li><a href="code/runtime_entrypoint.py">薄 HTTP 入口</a> / <a href="code/timex/runtime.py">请求隔离和 live 双开关</a></li>
<li><a href="code/timex/roles.py">角色利益</a> / <a href="code/timex/calling.py">分阶段提示词</a> / <a href="code/timex/graph.py">两轮协商图</a></li>
<li><a href="code/timex/gateway.py">一次 SigV4 MCP 取数和快照</a> / <a href="code/timex/gateway_tool.py">只读 Lambda 工具</a></li>
<li><a href="code/timex/memory.py">确认、更正、删除和当前请求优先</a></li>
<li><a href="code/timex/runtime_deploy.py">ARM64 制品白名单</a> / <a href="code/scripts/integrations_cloud.py">云端部署、Secret 上传与 fake 验收</a></li></ul>
<h2>不能据此宣称</h2><p>DeepSeek 云端质量已通过、Memory 是长期自动记忆、私人时间账户已接入、IAM 多用户隔离已完成、AWS 总费用为零。现有文章草稿、私人报告和任何凭证明文均未收录。</p>''')
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True,
                            capture_output=True, text=True).stdout.strip()
    files['manifest.json'] = json.dumps({'created_at': datetime.now(timezone.utc).isoformat(),
        'base_commit': commit, 'source_state': 'working tree including uncommitted code; no push performed',
        'files': manifest, 'generated_files': ['index.html', 'readable/*.html', 'notes/*.json'],
        'network_calls': 0, 'article_body_modified': False}, ensure_ascii=False, indent=2) + '\n'
    output.mkdir(parents=True, mode=0o700)
    for name, content in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        destination.chmod(0o600)
    return {'directory': str(output.resolve()), 'files': len(files), 'paid_model_calls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.output), ensure_ascii=False, indent=2))
