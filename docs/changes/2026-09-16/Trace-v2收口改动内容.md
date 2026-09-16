# P0-3 Trace v2 收口改动内容

日期：2026-09-16  
范围：P0-3 Trace v2；不提前实施 P0-2 / P1-4 / P1-6。

## 1. 目标与结论

本轮以 `dev@18fb0cc39b09d428a069b79c3f925875bcdd7ffe` 为实现起点，对 Forge Agent 现有 EventLog / Runner / Agent 生命周期做最小收口。

最终结论：P0-3 的实现门槛已满足，可以在 TODO 中标为 `DONE`；但当前 GitHub connector 没有可执行仓库环境，因此本轮没有声称 pytest 已通过。用户 pull 后必须执行文末命令做本地验证，若出现失败则重新打开 P0-3。

没有修改 `config/default.yaml`，没有重写 `evals/results` 历史 JSONL，没有修改 B1/B2 fixture，也没有引入 OpenTelemetry SDK、外部数据库或云 tracing 服务。

## 2. 设计

Forge 保留 append-only JSONL EventLog，Trace v2 不另建第二套 tracing backend。

统一层级：

```text
Run
├── Model Span
├── Tool Span
├── Context / Compaction Span
├── Completion Rejection
├── Termination
├── Acceptance
└── Delivery
```

核心原则：

1. Run 是一次 `ExecutionRunner.run()` 的统一 correlation 根。
2. Model / Tool / Context 是有持续时间的 child span。
3. Completion rejection 是可恢复的完成性事件，不等价于 run failure。
4. `run_terminated` 是 Runner 可见的最终统一终止记录；旧 `task_complete/task_failed/task_incomplete` 继续保留兼容语义。
5. Acceptance 是 Agent 完成后的独立验收层；Delivery 是 GitHub Issue 产品入口在 commit/push/PR 后追加的产品层结果。
6. 所有敏感信息最终都必须经过 EventLog JSONL 写盘边界的 schema-level redaction，不能依赖单个 Tool 自觉处理。
7. 本地 prompt token breakdown 只回答“为什么这一轮输入大”；provider usage 才是 provider 返回的真实 usage，两者不混为一个数字。

## 3. Trace v2 schema

新写入事件统一携带：

```text
trace_schema_version = 2
schema_version = 2          # 旧 reader 兼容别名
run_id
run_span_id
entrypoint
session_id
step_id / turn_id           # 有 step 时
span_id
parent_span_id
span_type
status
```

Run：

- `run_id`
- `run_span_id`
- `entrypoint`
- `started_at`
- `finished_at`
- `termination_reason`
- `resource_reason`
- `status`

Model span：

- `model_call_id`
- `provider`
- `model`
- `retry_count`
- `message_count`
- `tool_schema_count`
- `token_breakdown`
- `provider_usage`
- `error_type` / `status_code` / `error`
- `duration_ms`

Tool span：

- `tool_execution_id`
- `tool_name`
- `arguments`（写盘时统一脱敏）
- `result_bytes`
- `diagnostics`
- `error_type` / `error`
- `duration_ms`

Context/Compaction span：

- `compaction_id`
- `pressure_ratio`
- `before_tokens` / `after_tokens`
- `projected_input_tokens` / `projected_after_tokens`
- `summary_method` / `pruning_method`
- `summary_usage`
- `semantic_error` / `semantic_duration_ms`
- checkpoint lineage 字段

Completion：

- `completion_id`
- `code`
- `detail`
- `status=rejected`

Acceptance：

- `requested`
- `acceptance_status`
- `status=passed|failed|skipped`
- `error`

Delivery：

- `requested`
- `delivery_status`
- `status=delivered|failed|skipped`
- GitHub Issue 的 repo/branch/issue/PR 元数据

## 4. Correlation

`EventLog` 为一次 run 生成稳定的 `run_id` / `run_span_id`。

Model、Tool、Context span 都通过：

```text
parent_span_id = run_span_id
```

关联到 Run。start/retry/finish/failed 复用同一 `span_id`。

Compaction 与外层 `prepare_next_turn` 都属于 context 类 operation，但使用不同的 span family，避免嵌套 semantic compaction 错误复用 prepare span。

CLI / Chat / API / GitHub Issue 都通过 `ExecutionRunner` 解析统一 `entrypoint`。isolate 路径中 EventLog 由 orchestrator 深层创建，因此使用轻量 `ContextVar` 传播 entrypoint/session，而不是扩张 orchestrator 产品参数。

## 5. Redaction

新增统一 helper：`agent/trace_v2.py`。

写盘前递归处理：

- nested dict
- list
- tuple
- set
- free-form string / error message

敏感字段至少覆盖：

- `authorization`
- `proxy-authorization`
- `api_key` / `api key` / `apikey`
- `access_token`
- `refresh_token`
- `github_token`
- `password`
- `secret`
- `token`
- 常见 `_password` / `_secret` / `_token` / `_api_key` 后缀字段

字符串模式至少覆盖：

- `Bearer xxxxx`
- `sk-...`
- `ghp_...`
- `github_pat_...`
- `authorization: ...`
- `api key=...`
- `github token=...`

所有 payload 在 `EventLog._append()` 的最终 JSONL 边界统一调用 redaction。Action、Observation、Permission、provider error、Runner termination、GitHub delivery 等即使调用方忘记手工处理，也必须经过同一边界。

为了不破坏统计，以下类别显式保留：

- `input_tokens`
- `output_tokens`
- `cached_tokens`
- `cache_write_tokens`
- `reasoning_tokens`
- `total_tokens`
- token breakdown 中 system/tool schema/repo map/history/context/pending/injected/estimated input 等字段

因此不是“看到 token 字样就全删”。

## 6. Prompt / Context Token Breakdown

每次 Model span 的 `token_breakdown` 至少包含：

```text
system_tokens
tool_schema_tokens
repo_map_tokens
history_tokens
context_tokens
pending_tokens
injected_tokens
estimated_input_tokens
```

当前 Forge 的 system prompt 中包含 Repo Map，因此诊断时从 system message estimate 中扣出 repo map estimate，避免分区相加时重复计算。

`injected_tokens` 当前主要覆盖 ephemeral resource budget warning；`pending_tokens` 预留给未来确实存在的 pending request 内容，当前为 0。

Provider 返回继续单独记录：

```text
provider_usage.input_tokens
provider_usage.output_tokens
provider_usage.cached_tokens
provider_usage.cache_write_tokens
provider_usage.reasoning_tokens
provider_usage.total_tokens
provider_usage.estimated
```

兼容已有 B1/B2 reader，`usage` 暂时保留为同一 provider usage 的兼容别名。

## 7. 统一失败与终止口径

现有 Agent 的结构化终止原因继续作为事实来源：

- provider error → `provider_error`
- known runtime/infrastructure fatal error → `infrastructure_error`
- external cancel → `canceled`
- repeated loop termination → `loop_detected`
- max steps → `resource_exhausted` + `resource_reason=max_steps`
- completion guard rejection → `completion_rejected`，可恢复，不直接结束 run
- normal completion → `completion_satisfied`
- agent give up → `agent_gave_up`

Runner 在 Agent 返回后追加 `run_terminated`，保证上层统计不需要从多个 `task_*` 事件猜最终状态。

Runner 同时追加 acceptance；GitHub Issue 在 deterministic delivery 完成后追加 delivery。

## 8. 兼容策略

### 旧 JSONL

`EventLog.open_existing()` 可读取没有 Trace v2 metadata 的历史 JSONL。

旧行：

- 不迁移；
- 不重写；
- replay 保持原 payload。

若继续 append，新行才使用 Trace v2 metadata。

### RunResult

没有把 Trace v2 新字段设为 RunResult 必填参数；旧构造方式继续可用。

### B1 / B2

既有 reader 仍按 `event_type` 和可选 payload 字段消费：

- `evals/run.py::_count_tool_calls()` 只统计 `tool_execution_started`；
- B2 completion rejection reader 只统计 `completion_rejected`；
- aggregate 对旧 status/新 INCOMPLETE 已有兼容。

新增 `acceptance` / `run_terminated` / `delivery` 尾事件不会覆盖旧 `task_complete/task_failed/task_incomplete` 记录。

历史 `evals/results` 没有被本轮重写。

## 9. 修改文件

生产代码：

- `agent/trace_v2.py`：Trace schema 常量、ContextVar correlation、统一 redaction。
- `agent/task.py`：新增 `RUN_TERMINATED` / `ACCEPTANCE` / `DELIVERY` event type。
- `agent/event_log.py`：统一 schema enrichment、run/span correlation、redaction 写盘边界、acceptance/termination/delivery helper、旧 JSONL append 兼容。
- `agent/core.py`：Model span token breakdown、provider usage、provider/prepare/tool error trace 字段。
- `agent/runner.py`：四入口 entrypoint 解析、isolate correlation 传播、统一 acceptance/termination 与逃逸异常 trace。
- `entry/github_issue.py`：真实 delivery 结束后追加 delivery trace。

测试：

- `tests/test_trace_v2.py`
- `tests/test_github_issue_delivery.py`

状态与文档：

- `TODO-P0-P1.md`
- `Forge-Agent-P0-P1-实施计划.md`
- `AGENTS.md`
- `docs/README.md`
- 本文件

未修改：

- `config/default.yaml`
- B1/B2 fixture
- `evals/results/**` 历史结果

## 10. 测试覆盖

已新增/扩展代码覆盖：

1. schema version
2. run/span correlation
3. parent/child span
4. model span
5. tool span
6. compaction span
7. termination
8. acceptance/delivery
9. CLI / Chat / API / GitHub Issue entrypoint resolution
10. nested secret redaction
11. Bearer / API Key / GitHub Token redaction
12. usage token 字段不被误删
13. prompt token breakdown
14. provider error Trace
15. completion rejection Trace
16. INCOMPLETE Trace
17. 旧 JSONL 读取/追加兼容
18. GitHub delivery append correlation
19. B1/B2 既有 report/reader 回归由原测试继续覆盖

### 当前真实执行状态

本轮通过 GitHub connector 修改远程 `dev`。connector 能读写仓库和 commit，但没有仓库 shell/pytest 执行环境；容器环境也无法解析 `github.com`，因此本轮没有真正执行 pytest。

不能写成“测试已通过”。

## 11. 用户 pull 后测试命令

定向 P0-3：

```bash
pytest tests/test_trace_v2.py tests/test_runner.py tests/test_compaction.py \
  tests/test_agent_completion_guards.py tests/test_chat.py tests/test_api.py \
  tests/test_github_issue_delivery.py -q
```

B1/B2 reader 回归：

```bash
pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q
```

全量：

```bash
pytest -q
```

如果本地出现失败，先保留失败输出，不修改 fixture/历史结果；按失败节点重新打开 P0-3 定向修复。

## 12. 已知限制

- Trace v2 不是 OpenTelemetry implementation；只借鉴 trace/run/span 与 GenAI usage 的组织方式。
- 不做外部 collector、云 UI、数据库查询层或 distributed tracing。
- 不保存完整 provider request body 来做“可观测性”；避免重复大 payload 与扩大敏感面。
- 本地 token estimate 与 provider usage 的 tokenizer/协议开销可能不同，设计上故意分开。
- P0-2 的 Hook / Permission / Cancel 阶段级语义尚未收口，本轮只保证已有结果可被 Trace v2 一致记录。
- 不包含 Resource Manager、context_recall、MCP、多 Agent、multi-tool call。

## 13. 下一步：P0-2

下一对话从下面的问题开始，不提前实现 P1-4：

> 以 `harness/executor.py` 为中心，建立 `pre-hook → permission → tool → post-hook` 的真实执行顺序与四入口一致性矩阵；重点确认 cancel 在各阶段的语义，并把 permission/hook/tool/cancel 的失败稳定映射到 ToolResult、RunStatus 和 Trace v2 termination。

完成 P0-2 后再单独进入 P1-4 failure Harness。
