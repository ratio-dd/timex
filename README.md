# TimeX — M0

一个 AWS 无关、完全离线的四立场协商 POC。需求见 [docs/POC.md](docs/POC.md)。当前仅实现 M0：事业、探索、生活、健康各自提出意见，再读取共同的第一轮意见进行回应，协调器保留共识与分歧。最终决定由用户作出，不自动执行。

## 安装与运行

Python 3.11+；本云环境已用 Python 3.12.14 验证。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pytest -q
python -m timex.cli --csv examples/demo_events.csv
```

云环境已有 `/workspace/.venvs/timex`，可直接 `source /workspace/.venvs/timex/bin/activate`，在 `/workspace/timex` 运行后两条命令。

```bash
# 结构化结果；已有文件不会被覆盖
python -m timex.cli --format json --output outputs/result.json
# 单次调用的探索立场对照
python -m timex.cli --baseline --output outputs/baseline.json
# 完整可读报告
python -m timex.cli --output outputs/result.md
```

默认读取合成的 `examples/demo_request.json`。`--csv` 用内部标准 CSV 替换请求中的摘要；请求必须明确 7/30 天时间范围；`requested_minutes` 默认为演示所用的 180 分钟，其他议题请显式指定，不从自然语言猜测投入时长。CSV 的时间戳必须带时区，按摘要指定时区的本地日期截取。跨日记录裁剪、完全重复记录去重、重叠区间拒绝；未知分类转为 `other` 并提示，缺口计为 `untracked`，不视为休闲。`load_summary(..., category_map={...})` 支持分类映射。真实 aTimeLogger 导出需要以后单独增加适配器。

## fake 的含义与边界

fake 是实现 LangChain `BaseChatModel` 的本地规则模型，响应取决于摘要、显式投入时长、议题及同伴建议，不调用网络或付费模型。规则仅用于验证数据流：职业收益未知时不声称有收益；事业立场对明确非功利议题可建议零投入；探索先缩小实验；生活保护已有安排；健康在有足够记录且已记录睡眠较少时主张零新增投入。第二轮根据其他立场的具体时间上限缩小建议，也允许与零投入意见保持分歧。它不是自然语言理解器，也不能证明真实模型协商有效；没有医疗诊断。

固定串行 4+4+1=9 次调用，最多 10 次（`MAX_MODEL_CALLS` 可降低）；无自动重试或反思循环。`Limits` 可设置单次/总超时和输入/输出字符上限。fake 无 tokenizer，用字符边界控制长度，Token 用量保持 `null`，本地 fake 模型估算费用为 $0；不将字符数伪装成 Token 数。失败返回 `partial`、已经验证的轮次和错误状态，CLI 返回非零退出码。超时调用的 daemon 线程可能运行到该次本地调用结束，但不会重试或启动后续调用；M0 不是不可信代码隔离系统。

`MODEL_PROVIDER` 默认 `fake`，其他值和 `--live` 一律拒绝；没有 Bedrock/AgentCore SDK 或模型供应商实现。调用时关闭 LangSmith tracing。程序不保存原始 prompt/CSV 日志，私有数据、环境变量文件、凭证及输出目录忽略于 Git。合成报告见 [examples/demo-result.md](examples/demo-result.md)。

## 验证与后续

pytest 验证 CSV 聚合与坏数据、时区/跨日/DST/覆盖率、两轮事实隔离、9 次调用、预算与超时、坏响应、同伴意见引发修订、真实分歧、输出契约以及 CLI。GitHub Actions 在 Python 3.11/3.12 上执行锁定依赖安装、pytest 和 fake CLI；本地验证不代表 GitHub CI 已运行。

`examples/comparison-results.json` 给出冲突型、探索型、恢复型三个合成议题的单视角/多视角对照；请由用户人工评分，不预设多 Agent 更好。M1 真实模型、价格配置与真实质量评估，M2 AWS 部署、OIDC、清理，以及真实 aTimeLogger 导入均未实现。
