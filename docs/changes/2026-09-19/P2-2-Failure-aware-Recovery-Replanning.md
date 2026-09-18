# P2-2 Failure-aware Recovery + Replanning

日期：2026-09-19  
实现基线：`dev@1ad7850b8e84ebad650ccc3f16d46c791368f14a`  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 目标

P2-2 不新增第二个 Agent，也不把所有异常统一成 retry。

目标是在既有：

```text
Provider retry
Tool Observation
Reflection
Loop Detector
Completion Guard
P2-1 ExecutionPlan
```

之上增加可检查、可追踪、可评测的：

```text
failure evidence
  ↓
FailureContext
  ↓
RecoveryPolicy
  ↓
RecoveryDecision
  ↓
existing Agent loop
  ↓
continue / inspect / retry / retest / replan / terminate
```

Recovery 只决定“下一阶段应采用什么恢复策略”；真正的 Tool 调用仍由模型下一步 Action 触发，并继续经过 ToolExecutor。

## 实现

### 1. `agent/recovery.py`

新增：

- `RecoveryMode = off | structured`
- `FailureCategory`
- `FailureSource`
- `FailureContext`
- `RecoveryStrategy`
- `RecoveryDecision`
- `RecoveryPolicy`
- `RecoveryRuntime`

当前 FailureCategory：

- `TEST_FAILURE`
- `TOOL_FAILURE`
- `PERMISSION_DENIED`
- `LOOP`
- `NO_PROGRESS`
- `COMPLETION_REJECTED`
- `INFRASTRUCTURE`

当前 RecoveryStrategy：

- `RETRY`
- `INSPECT`
- `RERUN_TEST`
- `CHANGE_APPROACH`
- `REPLAN`
- `GIVE_UP`

首版 policy 是 deterministic，不调用额外 LLM classifier。

### 2. 不重复已有 retry / fatal contract

以下语义保持原实现：

- Provider transient error：`Agent._call_with_retry`
- cooperative cancel：直接 `CANCELED`
- ToolExecutor permission/confirm framework crash：`infrastructure_error`
- `prepare_next_turn` / context callback crash：`infrastructure_error`
- 已识别 Docker/runtime fatal error：继续使用 `fatal_error_key / fatal_tool_error_repeats`
- LoopDetector 第二次无进展 cycle：继续按既有 `loop_detected` 终止

因此 P2-2 不把基础设施故障伪装成“Agent 自己修好了”，也不在 RecoveryPolicy 内再做 Provider retry。

### 3. Recovery policy

主要 deterministic 规则：

```text
TEST_FAILURE
  first       -> INSPECT
  repeated    -> REPLAN (有 plan) / CHANGE_APPROACH (无 plan)

PERMISSION_DENIED
              -> CHANGE_APPROACH

LOOP
              -> REPLAN (有 plan) / CHANGE_APPROACH (无 plan)

NO_PROGRESS
              -> REPLAN (有 plan) / CHANGE_APPROACH (无 plan)

COMPLETION_REJECTED
  REQUIRED_TEST_MISSING
  FINAL_STATE_UNVERIFIED -> RERUN_TEST

  LATEST_TEST_FAILED     -> REPLAN / INSPECT
  REPOSITORY_UNCHANGED   -> REPLAN / CHANGE_APPROACH

TOOL timeout
  first       -> RETRY
  repeated    -> REPLAN / CHANGE_APPROACH

invalid/unknown/blocked tool path
              -> CHANGE_APPROACH
```

这里的 `RETRY` 只表示“下一步允许模型基于当前证据再尝试”，不会由框架自动重放上一 ToolCall。

### 4. Bounded recovery

`RecoveryPolicy` 维护 per-run recovery attempt budget。

超过 `recovery_max_attempts`：

```text
RunStatus = INCOMPLETE
termination_reason = recovery_exhausted
resource_reason = recovery_budget
```

防止结构化 recovery 自己变成无限恢复循环。

### 5. 与 P2-1 Replanning 的真实联动

当 RecoveryDecision 为 `REPLAN` 且当前已有 plan：

1. RecoveryRuntime 记录触发时的 `plan.version`；
2. read-only diagnosis 仍允许；
3. repository-mutating ToolCall 被 `RECOVERY_BLOCKED`；
4. FINISH 被 `RECOVERY_BLOCKED`；
5. 模型必须调用现有 P2-1 `plan_revise`；
6. 只有新 plan version 大于 failure 时记录的版本，gate 才解除。

因此：

```text
RecoveryStrategy.REPLAN
!=
“给模型一句请重新规划”
```

它对后续 mutation / finish 有 runtime enforcement。

### 6. Recovery state 与 Compaction

pending replan requirement 不只存在于 ConversationHistory。

`RecoveryRuntime.render_context()` 每轮把当前 gate 作为 bounded runtime system context 注入，与 P2-1 current plan 使用相同的“当前状态不依赖旧 history”原则。

因此即使 `prepare_next_turn` 使用 history override，当前 replan gate 仍可见。

首版没有新增独立 Recovery token breakdown 字段；这段文字计入普通 system diagnostic token，provider-reported usage 仍是权威 usage。

### 7. Trace v2

新增 point events：

- `failure_classified`
- `recovery_selected`
- `recovery_exhausted`
- `recovery_blocked`

统一 `span_type=recovery`。

`failure_classified` 保存 category/source/evidence summary/recent action summaries/repo/test/plan state；不保存新的第二份 trajectory。

### 8. Config 与产品入口

正式配置：

```yaml
agent:
  recovery_mode: "off"          # off | structured
  recovery_max_attempts: 4
```

默认 off，保证原有 ReAct / Reflection baseline 不被静默改变。

CLI / Chat / API / GitHub Issue 都把同一配置传入 AgentConfig。

### 9. P2-0 Evaluation Harness

正式 architecture mapping：

```text
baseline_react
  planning_mode=off
  recovery_mode=off

planning
  planning_mode=always
  recovery_mode=off

planning_recovery
  planning_mode=always
  recovery_mode=structured
```

TrialMetrics 新增：

- `failure_classified_count`
- `recovery_selected_count`
- `recovery_replan_count`
- `recovery_exhausted_count`

real-model aggregate 可记录这些过程指标；fake/scripted trial 仍不能生成 capability claim。

## Deterministic regression

新增 `tests/test_structured_recovery.py`，覆盖：

- deterministic policy 与 attempt budget；
- permission denied 不选择 unchanged retry；
- recovery off 保留旧 test Reflection；
- repeated test failure：INSPECT → REPLAN；
- REPLAN 后 mutation 真正被 gate；
- `plan_revise` version 提升后解除 gate；
- history override 后 recovery runtime state 仍保留；
- completion missing test → RERUN_TEST；
- recovery budget exhausted；
- infrastructure 保持既有 fatal contract；
- Eval variant mapping。

扩展 `tests/test_failure_harness.py`：

- structured recovery 不重复 Provider retry；
- Permission deny 经真实 Runner / ToolExecutor 路径产生 `change_approach`。

扩展：

- `tests/test_day6.py`：recovery config / YAML `off` 兼容；
- `tests/test_coding_agent_eval.py`：recovery Trace metrics extraction。

## 明确未做

- 不实现 RetryAgent / Reviewer Agent / Multi-Agent；
- 不自动重放失败 ToolCall；
- 不做环境 reset 后第二 attempt；
- 不把 cancel / infrastructure 当作智能恢复；
- 不新增 LLM-as-Judge；
- 不修改 Acceptance authority；
- 不实现 Skills / MCP / Evolution；
- 不运行或伪造 real-model A/B。

执行计划中原建议的 `CONTEXT_FAILURE` 没有作为首版智能 FailureCategory 接入，因为当前 `prepare_next_turn` / context policy 异常已经有明确的 framework infrastructure contract；把它改成模型可恢复错误会弱化现有失败边界。

## 本轮验证状态

当前 ChatGPT GitHub 执行环境没有用户本地 WSL checkout，无法运行仓库 pytest。

因此当前只记录代码实现与静态接口审计：

```text
P2-2
IMPLEMENTED / LOCAL VALIDATION PENDING
```

未执行：

- repository pytest；
- Docker E2E；
- real-model `planning vs planning_recovery` A/B。

## 本地验证

先运行新增/直接相关回归：

```bash
python -m pytest -q \
  tests/test_structured_recovery.py \
  tests/test_structured_planning.py \
  tests/test_failure_harness.py \
  tests/test_coding_agent_eval.py \
  tests/test_day6.py
```

再运行关键兼容回归：

```bash
python -m pytest -q \
  tests/test_agent_completion_guards.py \
  tests/test_loop_detector.py \
  tests/test_runner.py \
  tests/test_trace_v2.py \
  tests/test_compaction.py \
  tests/test_structured_compaction.py \
  tests/test_tool_lifecycle_p0_2.py \
  tests/test_evidence_pack.py
```

然后只验证 Eval variant 接线，不调用真实模型：

```bash
python -m evals.coding_agent \
  --variant planning_recovery \
  --output-dir evals/results/local-p2-2-recovery-not-executed
```

最后：

```bash
python -m evals.verify_evidence_pack
python -m pytest -q
```

没有 `--real-model` 的 eval 命令只生成显式 not-executed artifact，不提供任何 Recovery effectiveness 结论。
