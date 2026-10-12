# TimeX — Multi-Agent Deliberation POC

> **版本**：v0.1 · **日期**：2026-10-12 · **状态**：M0 和 M1 流程已验证，人工质量评价待完成
> **定位**：AWS 无关的个人多立场协商实验，在 Amazon Bedrock AgentCore 上验证运行。  
> **主要开发位置**：GitHub 仓库 + Codex Cloud；无需个人电脑保持开机或具备本地开发环境。

## 0. 一页概要

### 核心假设

用户在做生活与时间分配决策时，常有多个同时合理、互相冲突的诉求。让四个分别代表用户某一面立场的 Agent **独立表达、互相回应、协商让步**，也许可以产生比单个助手更平衡、更可解释的建议。

**本 POC 要检验的不是“AI 能否代替用户自律”，而是“Multi-Agent 讨论是否真的改变了方案，是否帮助用户看到被忽略的利益与权衡”。**

### 一次成功运行的样子

输入：
- 用户议题，例如「本周是否额外花 3 小时开发 TimeX？」；
- 真实或合成的 aTimeLogger 分类时间摘要（过去 7/30 天）；
- 用户提供的硬约束，例如本周旅行、固定活动、不得缩减睡眠。

输出：
1. 四个 Agent 的初始主张及理由；
2. 一轮交叉回应后各 Agent 的修订立场（或明确坚持原立场的理由）；
3. 一个**可执行但不自动执行**的联合建议，或无法达成一致的分歧说明；
4. 关键让步、证据与不确定性；
5. 本次执行的 LLM 请求次数、Token 用量（如可得）及估算成本。

**成功不是强制全体赞成**。保留真实分歧是一等结果。

---

## 1. 范围与边界

### P0：必须有

- 四个稳定的利益视角：`career`（事业）、`curiosity`（探索）、`life`（生活）、`health`（健康）。
- 所有 Agent 读取**相同的事实摘要**与用户议题；不得编造 aTimeLogger 未提供的事实。
- 两阶段讨论：独立立场 → 读取其他立场并修订；最后由无利益立场的协调器整理共识/分歧。
- 有明确的“其他 Agent 提出什么 → 我为何改变/不改变”的可追踪字段。
- CSV 转换成标准时间摘要；先用合成 CSV 跑通，真实 aTimeLogger 导出格式到手后再增加适配器。
- 本地/云端普通 Python 命令可运行（包括不调用任何云服务的 mock 模式）。
- 相同核心逻辑能部署到 Amazon Bedrock AgentCore Runtime，并从 AWS 云端被调用。
- 无需 Web UI；CLI + JSON/Markdown 结果足够。
- 最小自动化测试与单 Agent 对照实验。

### P0：明确不做

不做货币、竞价、党派预算、投票、政治联盟、自适应权重、长时记忆、复杂状态机、自动修改日历、持续监控、aTimeLogger 自动同步、手机 App、数据库、向量库、自动化部署到生产、复杂前端。

**实现原则**：新想法先写入 `docs/ideas.md`，不得自动进入 P0；不要为了展示更多 AWS 服务而添加组件。

---

## 2. 角色与讨论规则

| Agent | 必须代表的利益 | 应当考虑但不预设的观点 |
|---|---|---|
| `career` 事业 | 职业积累、工作、收入、对未来选择权的影响 | 不总是要求更多工作，也可以反对低收益任务 |
| `curiosity` 探索 | 好奇心、技术学习、创造和非功利探索 | 可以支持纯娱乐式探索，也可以主动缩小项目 |
| `life` 生活 | 娱乐、关系、自由时间与当下体验 | 娱乐不自动等于浪费；不总是反对学习 |
| `health` 健康 | 睡眠、恢复、身体状态与可持续性 | 不作医疗诊断；可以支持令人愉悦的活动 |

### 讨论流程（固定两阶段）

1. **Round 1 / 独立立场**：四个 Agent 各自输出 `position`、`reasons`、`concerns`、`acceptable_conditions`、`evidence_refs`、`unknowns`。彼此不看见对方的初始输出，防止先入为主。
2. **Round 2 / 交叉协商**：每个 Agent 收到四份 Round 1 立场，输出 `revised_position`、`responses_to_others`、`concessions`、`remaining_objections`、`change_explanation`。可以坚持初始立场，但必须解释。
3. **Synthesis / 结果整理**：协调器只负责提炼协议、未解决分歧、可选折中方案与证据；不得伪造全体共识。最终决策权属于用户。

不引入额外的投票或数值效用公式。若第二轮仍有分歧，直接呈现；最多提供一份标明“待用户裁决”的折中建议。

### 防止“剧本式讨论”

- 角色是利益视角，不是固定支持/反对立场；不允许 `career=永远赞成` 这类硬编码。
- 每个修订必须引用至少一个其他 Agent 的具体观点，或明确指出没有新信息足以改变主张。
- 不能凭空写「一致通过」「各方满意」；须以 Round 2 的真实输出为依据。
- 对健康、就业等议题，保留不确定性，不把推测当事实；不得诊断或作不可逆决策。
- 用户偏好高于模拟 Agent 的协商结果；永远允许拒绝。

---

## 3. 数据与隐私

### 规范化数据（不要猜测 aTimeLogger 的原始列名）

在真实导出文件未知的情况下，先定义内部统一事件格式：

```csv
start_at,end_at,category
2026-10-05T09:00:00+08:00,2026-10-05T18:00:00+08:00,work
2026-10-05T20:00:00+08:00,2026-10-05T21:00:00+08:00,leisure
```

内部类别最少支持 `work`、`learning`、`leisure`、`sleep`、`exercise`、`social`、`other`、`untracked`。映射规则可通过简单配置调整；**不要求用户为了 POC 重新分类旧数据**。

CSV loader 要处理：时区、跨日活动、空值、无效时长、重复记录、重叠区间；记录缺失不自动当作休闲。报告注明记录覆盖率、数据时间范围及可能存在的分类偏差。对重叠记录选择明确的保守策略（例如拒绝或标注），不能静默双重累计。

### 输入上下文

可接受一个 `deliberation_request`：

```json
{
  "question": "这周要不要额外花3小时开发TimeX？",
  "time_window_days": 7,
  "constraints": ["本周五出行", "本周六有线下活动", "不挤占睡眠"],
  "time_summary": {
    "period_start": "2026-10-05",
    "period_end": "2026-10-11",
    "category_minutes": {"work": 2400, "learning": 180, "leisure": 900, "sleep": 3360},
    "tracked_minutes": 6840,
    "untracked_minutes": 3240,
    "notes": ["以上为演示合成数据，不是用户实际记录"]
  }
}
```

上面的数值是**合成样例**，不能当成真实用户画像。

### 隐私默认值

- 仓库只提交**虚构示例数据**。真实 CSV、用户私人日程和原始完整对话不进入 Git 提交历史。
- `.gitignore` 排除 `data/private/`、`.env`、各种凭证文件、运行原始日志。
- 若要发送到云端模型，仅发送满足分析需要的**聚合分类摘要 + 用户手动提供的约束**，不默认上传完整的 7×24 原始事件流水。
- 日志仅保留执行 ID、角色、轮次、用量、延迟与结果状态；默认不打印完整私人 prompt 或原始 CSV。
- Codex Cloud 中的文件依平台设置处理；正式使用真实个人数据前应确认共享和保留策略。

---

## 4. AWS 无关的系统设计

### 推荐架构

- **语言**：Python 3.11+。
- **编排**：LangGraph；业务节点使用 LangChain 的 ChatModel 抽象。
- **模型注入**：业务层只依赖模型调用接口；支持 `fake`（测试）、`bedrock`（AWS 实测），以后可添加其他 LangChain provider。
- **数据层**：纯 Python CSV 聚合器。
- **部署层**：AgentCore entrypoint 只负责请求校验、调用 `run_council()`、返回结果。
- **持久化**：P0 不需要数据库。一次运行内保存结构化状态；输出 JSON 和可读 Markdown 即可。

```text
                         用户议题 + 时间摘要
                                  |
                         run_council() 纯入口
                                  |
                     LangGraph 协商工作流
                     /       |       |       \
                 career curiosity   life    health
                     \       |       |       /
                       Round 1 独立输出
                                  |
                      Round 2 交叉回应
                                  |
                          协调器综合
                                  |
                          JSON / Markdown
                                  |
           +----------------------+------------------+
           |                                         |
    云端普通 Python CLI                      AgentCore Runtime 适配器
    (mock/其他模型供应商)                       (Bedrock 模型)
```

第一版可以顺序执行四个角色，后续若简单再行并发；避免为并发引入复杂的同步、重试和状态共享机制。

### 建议的最小代码布局（可按实际框架调整）

```text
timex/
  models.py               # 输入、各轮主张、最终输出的数据模型
  roles.py                # 四个角色的稳定利益提示词
  graph.py                # 两轮 + synthesis，LangGraph 工作流
  providers.py            # LangChain 模型工厂，fake/bedrock
  time_data.py            # CSV 规范化和统计
  cli.py                  # 本地和 Codex Cloud 可执行入口
  aws_entrypoint.py       # 唯一与 AgentCore Runtime 耦合的文件
examples/
  demo_events.csv         # 合成数据
  demo_request.json
  expected_shape.json
tests/
  test_time_data.py
  test_graph_mock.py
  test_contract.py
docs/
  POC.md
  ideas.md
.github/workflows/
  ci.yml
  deploy-aws.yml         # 手动触发、需审批
```

（实际建立仓库时修正上面的展示缩进即可。）

### 结构化输出契约

最终输出至少包含：

```json
{
  "question": "...",
  "round1": {"career": {}, "curiosity": {}, "life": {}, "health": {}},
  "round2": {"career": {}, "curiosity": {}, "life": {}, "health": {}},
  "shared_ground": ["..."],
  "proposed_plan": {"text": "...", "conditions": ["..."]},
  "unresolved_conflicts": ["..."],
  "requires_user_decision": true,
  "evidence_limitations": ["..."],
  "usage": {"model_calls": 9, "input_tokens": null, "output_tokens": null, "estimated_usd": null}
}
```

`usage` 中的 `null` 表示供应商没有返回该指标；不得伪造数字。

---

## 5. 云端研发与 AWS 部署边界

### 开发链路（无本地电脑依赖）

1. 在 GitHub 创建 **private** 仓库，将本文提交为 `docs/POC.md`。
2. 在 Codex Cloud 连接该 GitHub 仓库，要求先完成 `fake` 模式、静态样例和测试；在最初两个阶段**不授权 AWS 部署**。
3. 使用 GitHub Actions `ci.yml` 在 PR 上运行 lint / pytest / mock 协商；CI 默认不访问 Bedrock、不部署 Runtime。
4. AWS 初始信任配置由用户在浏览器中的 AWS Console / CloudShell 完成；不需要本地 AWS CLI。
5. 通过 GitHub Actions OIDC 从指定仓库与指定分支/Environment 换取 AWS **短期凭证**，部署动作使用受限 IAM Role。
6. `deploy-aws.yml` 仅 `workflow_dispatch` 手动触发，并使用受保护 Environment；首次部署必须人工确认。
7. 在云端手动运行 1–3 次真实 AgentCore 验证后停止；P0 不设置定时调用。

**原则**：Codex Cloud 是编码环境，GitHub Actions 是 AWS 部署执行环境。不要把长期 AWS Access Key 放进 Codex 环境或 Git 仓库。OIDC 身份信任需限定仓库、分支/Environment，授权仅部署所需权限。

### AgentCore 路线（核对 2026-10 官方文档）

- 使用 **AgentCore CLI** 的 code-based agent 模式承载 LangGraph，不使用 AgentCore managed harness 取代核心工作流。
- 官方新项目推荐 CLI：`npm install -g @aws/agentcore`。旧 `bedrock-agentcore-starter-toolkit` CLI 已不再是新项目的推荐起点。
- 部署建议 Runtime **microVM consumption-based**，**direct-code / CodeZip** 优先；避免 Instances、常驻服务和额外容器体系。
- 先通过 CLI 的 `dev`/`validate`/`deploy --dry-run` 及当前版本生成的配置检查部署计划；不要在未经确认时执行 `deploy`。
- 只在 `aws_entrypoint.py` 引入 AgentCore SDK。`graph.py` 不导入任何 `bedrock_agentcore` 相关包。
- 云端首轮验证仅部署 **一个 Runtime 项目**；四个逻辑 Agent 不等于四个独立 AWS 服务。
- 模型供应商默认 Bedrock，选部署区域支持、权限可用且价格较低的轻量文本模型；**不要硬编码某个未经实际验证可用的模型 ID**。
- 先只创建 Runtime 必需资源；不启用 AgentCore Memory、Gateway、Browser、Code Interpreter、托管 Evaluations、OpenSearch、数据库、复杂 CloudWatch tracing 采样。
- 完成验证后检查 CLI 资源删除计划，清理 Runtime 及残留的构建/日志资源（CloudWatch 日志组等可能需要单独删除）。

---

## 6. 严格成本保护（P0 必须）

### 费用来自四个方向

1. **LLM 推理**：Bedrock 输入/输出 token；模型与区域决定价格。多 Agent 往往是最值得优先约束的开销。
2. **AgentCore Runtime**：按实际 CPU/内存消耗计费，不能把它简单理解为免费的托管入口。
3. **周边资源**：CloudWatch Logs、代码存储 S3、可能的构建资源及流量。
4. **CI / Cloud 工具**：GitHub Actions / Codex Cloud 的用量及配额，按各自账户计划计算。

官方 2026-10 的 AgentCore Runtime microVM 定价表：v2 consumption CPU **$0.1276/vCPU-hour**、内存 **$0.0169/GB-hour**；v1 CPU **$0.0895/vCPU-hour**、内存 **$0.00945/GB-hour**。实际启用版本、区域、服务变更及周边费用以 AWS 账单与定价页为准。**以上不是每次议会的固定价格。**

### 软件内限制（比云端账单告警更重要）

- 默认 `MODEL_PROVIDER=fake`；CI 永远使用 fake，测试不触发真实付费模型。
- 每次议会理论上 4（独立）+ 4（回应）+ 1（综合）= **9 次模型调用**。
- P0 限制 `MAX_MODEL_CALLS=10`：留 1 次格式错误重试空间，禁止自主循环、无限反思、自调用和额外审查 Agent。
- 每 Agent 初始/修订输出控制在短篇幅；传入的是时间摘要，不是原始 CSV 全文，也不是长对话历史。
- 设定单轮 Token 上限、单次调用超时、总执行超时和最大输出长度；一旦触发上限，返回部分结果及错误标记，不追加调用。
- 实现**调用前估算 + 调用后计量**（若模型提供 usage）。`MAX_ESTIMATED_USD_PER_RUN=0.25` 为建议初始软上限，可调整；估算不应伪装成 AWS 硬封顶。无法确认模型单价时，默认拒绝开启 live 模式，要求用户确认价格配置。
- live 模式必须显式输入 `--live` 或等价开关；默认不开启。对于 GitHub Actions，AWS smoke test 必须手动触发，不设每次 PR 自动调用 Bedrock。
- 默认串行；`max_concurrency=1`；关闭自动长重试，网络错误最多一次有限重试，并记入调用预算。
- 只记录最小必要的日志和短保留期；日志中不得包含完整原始时间流水。

### 账户级保障

- 建议给此实验设置**每月 $5 的 AWS 成本预算**，按实际支出 50%/80%/100% 通知；这是示例预算，不表示真实成本一定低于 $5。
- **AWS Budgets 通常是延迟的告警机制，不是实时硬性停机开关**，可能存在数小时延迟；不可仅依赖它控费。若账户具有项目级 spend limits 等更强约束，可另行评估。
- 禁止自动重复部署、定时轮询 Agent、自动大量评估；测试完成后删除不再使用的资源。

### 简单成本测算方法

```text
Model cost ≈ input_tokens / 1_000_000 * selected_model_input_rate
           + output_tokens / 1_000_000 * selected_model_output_rate
Runtime cost ≈ consumed_vcpu_seconds / 3600 * runtime_cpu_rate
             + billed_GB_seconds / 3600 * runtime_memory_rate
Total ≈ Model + Runtime + Logs + Storage + DataTransfer + other services
```

模型实际单价由部署时选择的 Bedrock 具体模型、区域、推理层级确认后填写，本文不杜撰。

---

## 7. 测试与评估：证明“讨论”不是装饰

### 自动测试：不要花模型钱

- `test_time_data.py`：分类聚合、跨日、时区、缺失、重叠处理和记录覆盖率。
- `test_graph_mock.py`：四个角色均参与 Round 1/2，调用量受限，没有无限循环；模拟互相回应。
- `test_contract.py`：输出包含每个角色的初始/修订、真实分歧、成本元数据；不允许强制假共识。
- 契约验证及测试都使用 fake 模型，不调用 AWS。

### 极简人工对照（仅 3 个议题）

1. **冲突型**：本周临时新增 3 小时 Agent 开发，而日程已有固定事项。
2. **探索型**：一个无明显职业收益但真的有趣的技术探索是否值得安排。
3. **恢复型**：工作后精力较低时，是否应该减少学习转为休息。

每个议题运行：单 Agent 建议 1 份，多 Agent 协商 1 份。用户从下列 4 项中评分（1–5）并留下简单备注：
- 是否覆盖我真正关心的几种利益？
- 是否回应彼此意见，而不是平行陈述？
- 是否呈现了可解释的让步或真正的分歧？
- 最终方案是否比单 Agent 更有帮助？

记录调用次数、Token、实际开销、总运行时间。不以“多 Agent 必须胜出”为结论；若无明显增益，记录反例，也是一项有效的技术结论。

---

## 8. 可验收的里程碑（Codex Cloud 的实施顺序）

### M0 — 可运行的假数据版本（无 AWS，优先完成）

- [x] GitHub 仓库初始化；本文、README、`.gitignore`。
- [x] 输入/输出类型与四个角色 prompt。
- [x] LangGraph 两阶段议会 + synthesis。
- [x] `fake` 模型、合成 JSON/CSV、CLI 入口。
- [x] pytest 通过，CI 不调用云端模型。
- [x] 产生一份可读的 `demo-result.md`，展示至少一个立场因他人意见发生改变，或合理地保留分歧。

**验收**：在 Codex Cloud 执行一个命令即可跑完整闭环；不需要 AWS 凭证、不开新服务。

### M1 — 真实模型、但仍 AWS 无关

- [x] 一个可替换的 LangChain ChatModel provider；可通过环境变量选择。
- [x] 一轮真实模型协商，结构化结果稳定；有效限制最大调用次数和 Token。
- [x] 实测 1 个多 Agent vs 单 Agent 的案例；记录成本与时延。
- [x] 有错误时可有限重试或返回部分结果，不能无界循环。

**验收**：无需 AgentCore 也能运行同一业务逻辑；至少有 1 份真实模型协商报告。

### M2 — AgentCore 云端验证

- [ ] AWS Region/模型/权限/价格经人工确认。
- [ ] GitHub OIDC 部署角色与受保护环境配置完成。
- [ ] 用最新 AgentCore CLI 把**同一核心**部署为单个 Runtime。
- [ ] 通过云端手动触发一次真实议会请求，输出与 M1 相同的数据结构。
- [ ] 截图/保存去敏的结果、成本与调用记录；验证清理流程。

**验收**：完整交付“GitHub 源码 + Codex Cloud 开发 + AgentCore 在线调用”的演示，且没有把 AWS SDK 依赖渗透到协商核心。

### M3 — 可写成 AWS UG 技术文章（非 POC 完成的阻塞条件）

产物不是营销介绍，而是一篇基于真实工程实验的文章，包含架构图、协商案例、单 Agent 对照、AgentCore 部署经验、Token/Runtime 成本结构、失败或局限。

**注意**：若 UG 投稿有紧迫截止日期，**优先完成 M0/M1 或写设计与实验计划**；不要为赶稿仓促授予云端部署权限或虚构 AWS 实测结果。

---

## 9. 建议的 AWS UG 文章框架

**暂定标题**：《让四个 Agent 代表我的不同利益开会：基于 LangGraph 与 Amazon Bedrock AgentCore 的个人决策 POC》

1. 为什么单一的时间管理 Agent 未必能够表达同一个人的全部诉求。
2. 四个稳定利益视角 + 两阶段真实协商，为什么不用投票和复杂规则。
3. AWS 无关的 LangGraph 核心、Bedrock 模型和 AgentCore Runtime 的职责边界。
4. 一次真实议题运行：初始立场、反驳、立场修订、未解决分歧。
5. 成本：九次推理调用如何限制，Runtime 与 Token 哪部分更贵（依据实测）。
6. 单 Agent 对照与失败案例：何时不值得用 Multi-Agent。
7. 下一步，不扩大当前 POC 的验收范围。

在实际采集数据前，任何价格、延迟、截图或质量评价都只能标注为**示例/预计**。

---

## 10. 给 Codex Cloud 的执行指令（直接复制）

> 你正在 GitHub 仓库内实现 TimeX POC。首先完整阅读 `docs/POC.md`。
>
> **只实现 M0；不要未经要求推进 M1/M2。** 使用 Python + LangGraph/LangChain，保持议会核心与 AWS 无关。四个角色分别代表事业、探索、生活、健康。完整跑通独立立场、交叉回应、共识/分歧整理两个讨论阶段，并保存结构化结果。使用 fake model 和合成 aTimeLogger 数据，编写 pytest 与 GitHub Actions CI。不要创建 AWS 资源、不要索取 AWS 长期密钥、不要调用付费模型、不要引入数据库/UI/持久化/额外 Agent。
>
> 开始改代码前，先列出 3–5 个你准备交付的文件/结果。完成后实际运行测试，提供执行命令、测试结果、示例输出及限制；优先提交一个小而可验证的 PR。若遇到不确定的 aTimeLogger 格式，用内部标准格式与适配接口，不猜造真实导出列名。不要为了“架构完美”扩大范围。

---

## 11. 已验证的官方参考链接（核查日期：2026-10-12）

- [AWS AgentCore CLI Get Started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html) — 新 CLI、Code-based agent、验证、部署、清理。
- [AWS AgentCore Runtime 支持框架](https://aws.amazon.com/bedrock/agentcore/faqs/) — LangGraph 等框架与模型选择。
- [AWS AgentCore 官方定价](https://aws.amazon.com/bedrock/agentcore/pricing/) — Runtime microVM 与各类服务单价。
- [Amazon Bedrock 模型定价](https://aws.amazon.com/bedrock/pricing/) — 具体模型单价按区域与模型确认。
- [AgentCore Runtime 生命周期参数](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html) — idle timeout、会话生命周期。
- [GitHub Actions AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws) — 无长期静态 AWS key 的部署身份方案。
- [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) — 费用告警与延迟限制。
- [LangChain LangGraph 自定义多 Agent 工作流](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow) — 自定义顺序/并行工作流。

**变更控制**：官方 SDK/CLI 更新很快。Codex 实施 M2 时重新核对最新 CLI 说明和区域/模型可用性，不机械复制旧的 Starter Toolkit 命令。
