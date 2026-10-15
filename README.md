# TimeX

让四个 Agent 分别从事业、探索、生活、健康的角度讨论个人时间安排。它们先独立提出建议，再回应彼此意见，最后由协调器整理方案、让步和分歧，交给用户决定。

当前已实现两轮 LangGraph 协商、CSV / aTimeLogger 时间摘要、离线演示、OpenAI 兼容模型接入，以及同议题的单助手对照。M1 工程验收已完成，包含合成冲突/恢复情境的真实模型对照；aTimeLogger 真实摘要已在云环境完成只读验证。用户授权后的真实数据模型对照也已完成，人工质量评价仍待完成。完整计划见 [docs/POC.md](docs/POC.md)，M1 配置与验收见 [docs/M1.md](docs/M1.md)。

## 安装与运行

Python 3.11+。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-live.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pytest -q
python -m timex.cli --csv examples/demo_events.csv
```

云环境已有 `/workspace/.venvs/timex`，可运行 `source /workspace/.venvs/timex/bin/activate`，然后在 `/workspace/timex` 执行 CLI。仅需 fake 演示时也可安装 `requirements-dev.lock`。

```bash
# 四立场协商，保存 JSON
python -m timex.cli --format json --output outputs/council.json
# 一个助手同时考虑四项利益
python -m timex.cli --baseline --output outputs/single.json
# 同议题的多 Agent / 单助手对照，保存可读报告
python -m timex.cli --compare --output outputs/comparison.md
# 结构化对照结果
python -m timex.cli --compare --format json --output outputs/comparison.json
```

默认使用 `MODEL_PROVIDER=fake` 和合成的 `examples/demo_request.json`。CLI 会保留已有输出文件；再次运行时请选择新路径。JSON 与 Markdown 是两种输出格式，上面的两条对照命令各自运行一次实验；已有 JSON 可按 [M1 文档](docs/M1.md) 转成 Markdown。

## 使用 DeepSeek 等兼容服务

推荐在 GitHub 的 `test-env` Environment 中保存 `DEEPSEEK_API_KEY`，手动运行 **DeepSeek M1 smoke** 工作流。默认 `check` 模式仅验证 Secret 是否存在及价格配置；选择 `live` 后使用 `deepseek-flash` 跑一次合成案例对照，并提供 JSON / Markdown 下载；配置步骤见 [M1 文档](docs/M1.md#github-actions-实测推荐)。

工作流已包含公开价格默认值；GitHub Variables 可选，用于覆盖价格配置。API Key 使用 Environment Secret。

普通 Python 环境也可运行：通过环境 Secret 设置 `OPENAI_API_KEY`，然后执行：

```bash
# 载入模型名、端点、输出模式及当前保守价格配置
source examples/deepseek.env.example
python -m timex.cli --live --compare --format json --output outputs/live-comparison.json
```

配置示例使用 `deepseek-flash`，对应 V4.1 Flash，依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/quick_start/pricing/)（2026-10-12 核对）。示例按高峰、缓存未命中价格配置输入 $0.30、输出 $1.20／百万 Token，并关闭思考模式以配合短输出预算。显式加入 `--live` 后发起真实请求。报告保存两种方案、同一份摘要与约束、调用次数、Token、按配置价格估算的费用、时延和待填写的人工评分。

## 运行限制与结果

议会通常调用 9 次，最多 10 次；格式错误和临时网络错误合计最多重试 1 次。单助手通常调用 1 次，最多 2 次。对照中的两次运行分别应用预算，默认每次费用估算上限 $0.25，合计最多 $0.50 的软预算。调用失败或触发限制时返回 `partial` 和已验证的结果，CLI 退出码为 1；议会未完成时跳过单助手调用。

每次调用都有输入估算、输出 Token 上限、字符限制与超时。SDK 自动重试已关闭。供应商返回的 Token 用于调用后计量；缺失计量时报告 `null`，预算继续保留保守估算。配置项及估算边界见 [docs/M1.md](docs/M1.md)。

新计划分别显示默认建议、条件满足后的总上限和增加投入的触发条件；契约要求默认暂缓用0分钟表示，正文一致性仍需人工审阅。历史报告明确保留旧字段语义，详见 [计划契约](docs/M1.md#计划时长契约-v2)。

## 时间数据

`--request` 指定议题 JSON，`--csv` 用内部标准 CSV 替换其中的时间摘要。请求明确指定 7/30 天范围；`requested_minutes` 默认 180 分钟，其他议题可显式修改。若已知未来可投入的硬上限，可另填 `available_minutes`；此值同时约束默认建议、条件上限与角色建议，未知时不填。CSV 使用 `start_at,end_at,category`，时间戳带时区，按摘要指定时区的本地日期截取。

聚合器裁剪跨日记录、去除完全重复记录、拒绝重叠区间，并报告覆盖率。未知分类归入 `other`，缺口归入 `untracked`。`load_summary(..., category_map={...})` 支持分类映射。aTimeLogger 只读历史适配器使用区间并集，跨类别重叠归入 `other`，使用方式及分类变更规则见 [docs/ATIMELOGGER.md](docs/ATIMELOGGER.md)。

## 示例与验证

[离线对照报告](examples/m1-comparison-fake.md) 展示完整流程；fake 是基于摘要与同伴意见的规则模型，用来验证数据流。真实讨论效果需结合 live 报告人工评价。

pytest 覆盖 CSV、事实隔离、交叉回应、分歧契约、调用与费用预算、Token 计量、重试、超时、provider 和 CLI。CI 在 Python 3.11/3.12 上运行 fake 流程，provider 测试通过内存 HTTP 响应验证 SDK 行为。

真实输入放在 `data/private/`，运行结果放在 `outputs/`，凭证通过环境 Secrets 提供；这些本地路径已加入 Git 忽略规则。模型接收分类摘要和手动约束。M2 的 AgentCore 部署、OIDC 和云端验证按后续里程碑推进。


## 今晚的文章与 demo

当前重点是协商体验与AWS实践分享。阅读[中文文章编辑稿](docs/ARTICLE.zh-CN.md)、[5分钟demo讲稿与角色soul草案](docs/DEMO.md)、[已有合成情境的完整真实模型协商](docs/demo/conflict-transcript.md)。用户反馈目前像四个人附和，人设/soul不足是待验证假设；本次仅整理文档，不更改角色prompt。

AgentCore Runtime、Gateway、Memory尚未实测；文章相应章节明确标为设计。模型继续使用外部DeepSeek，源码保持公开，私人报告不进入仓库。路线见#7–#14。
