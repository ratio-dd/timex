# aTimeLogger 只读摘要

`scripts/check_atimelogger.py` 使用固定 atimelogger-mcp 0.2.0 CLI 验证认证与汇总，不打印私人名称、时长、备注或错误响应。托管代理环境使用 Node 24；脚本为子进程开启 `NODE_USE_ENV_PROXY=1`，保留环境代理与 CA。安装可指定可写缓存：

```bash
npm ci --prefix tools/atimelogger --cache /workspace/work/npm-cache --ignore-scripts --no-audit --no-fund
```

`timex.atimelogger` 根据同版本作者源码中的 API 契约读取用户时区、类别、历史区间、统计及当前活动。区间查询和统计查询使用 POST，但都是只读查询，没有修改活动的入口。原 CLI 将 Unix 秒格式化为没有时区的分钟级时间文本，因此摘要使用同一历史 API 的精确 `from` / `to` Unix 秒，避免秒数损失和 DST 歧义。原始区间、备注、标签只在内存中存在，不存文件或进入模型提示。

## 运行

凭证仍命名 `ATIMELOGGER_ACCESS_TOKEN`，直接从进程读取，不写入文件。先在 `data/private/atimelogger-mapping.json` 保存私人分类配置。下面只是假设的类别配置，不代表真实账户：

```json
{
  "职业": "work",
  "职业/培训": "learning",
  "自学": "learning",
  "id:example-stable-category-id": "exercise"
}
```

```bash
python scripts/atimelogger_summary.py \
  --mapping data/private/atimelogger-mapping.json \
  --timezone-fallback Asia/Shanghai \
  --output outputs/time-summary.json
```

默认读取账户时区中最近 7 个完整日，不包括今天。账户时区缺失时必须显式给 fallback；时区来源写入摘要，不采用云主机时区静默兜底。可用 `--period-end YYYY-MM-DD` 固定最后一天。输出已有文件不覆盖，权限设为 0600；命令只打印固定成功或失败信息。

JSON 中 `time_summary` 可以直接作为 `DeliberationRequest.time_summary`，其他字段是私下审阅用的映射审计、每日聚合及覆盖诊断。议题、`requested_minutes` 和约束应另外提供，不继承 demo 的合成日程。此入口不调用模型，CI 继续使用离线数据。

## 分类以后改变时

- 配置独立于代码，修改映射不需要重新分类旧流水。优先将已确认类别或父组写成 `id:<ID>`，同一 ID 改名后映射不变；路径规则用于初始配置和按名称匹配。
- 从根到叶继承映射，较近的类别覆盖祖先，同一层 ID 规则优先于路径。组仅用于映射，不贡献时长。移动类别后会采用新的父组默认；需要固定语义时给叶子明确 ID 规则。
- 新子类别继承父组，新根类别或不匹配类别先归 `other`，`mapping_audit[].defaulted` 提醒审阅。删除再新建产生新 ID，不能自动认作旧类别；复制的同名类别可用各自 ID 区分。
- 缺失父组不猜测历史名称，使用明确的叶子规则，否则 `other`，审计标记 `orphaned_parent`。循环关系或重复 ID 会停止。
- 每份结果保存本次解析后的类别路径与映射结果，旧摘要不会因以后配置变更而自动重写。审阅缺省映射、变组后的语义和新增类别后，再将聚合摘要用于模型。

## 区间覆盖与不确定性

本地午夜按时区转为 Unix 秒，区间裁剪至日期窗口，日界按 DST 实际长度计算。相同区间 ID 的重复响应去重，同 ID 不同内容拒绝；非有限时间、负时长及明显的时间戳/时长不一致均拒绝。历史分页有明确结束标志和最多 10 页限制，不用第一页面冒充完整历史。

覆盖率使用所有区间的并集。同类别重叠只计一次，不同类别同时记录的片段保守归入 `other`，报告重叠并集时长。每个时间片只属于一个标准类别，符合现有 TimeSummary 总量契约。`other` 同时包含日常杂项、缺省映射和重叠歧义，不能当成空闲或浪费；`untracked` 是窗口减并集，类别为零不等于实际行为不存在。

同时记录 API 统计总量与原始裁剪时长、并集总量的差，不靠 API 数值相加推导空闲。统计与并集差超过 1 秒时提示核对。正在运行或暂停的活动只记录计数，不从开始时间外推；完整过去日期采用历史已返回区间。睡眠类别可能含休息、误记或跨类别歧义，不能据此诊断。

离线测试验证重叠、跨界裁剪、DST、重复冲突、分类继承与改名、新/缺失类别、分页及私人错误脱敏。真实账户的类别、映射配置、每日数值和报告不进入公开仓库或 Actions 日志。
