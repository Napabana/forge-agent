# P0-2 Tool Hook / Permission / Cancel 收口改动内容

日期：2026-09-16  
基线：`dev@adb1252884743804610923f9c27a9849a4f9ba4b`

## 目标

本轮只收口已有 Tool lifecycle，不新增 Hook framework，不扩到 P1-4、Resource Manager、async Tool 重写、MCP、多 Agent 或 multi-tool call。

最终目标是让 CLI、Chat、API、GitHub Issue 通过统一 Runner / Agent / ToolExecutor 获得一致的：

`validate → pre-hook → permission → tool → post-hook`

失败分类、cooperative cancel 和 Trace v2 语义。

## 原执行链

审计基线时实际行为是：

1. Agent 在 step 顶部检查一次 cancel；收到 TOOL_CALL 后、进入 executor 前再检查一次。
2. ToolExecutor 先做 tool name / arguments validation。
3. PreToolUse hook：明确 block 返回 `HOOK_BLOCKED`；hook callback 抛异常返回 `HOOK_FAILED`。
4. PermissionManager 若存在则执行 permission；deny/confirm 拒绝返回 `PERMISSION_DENIED`。
5. ToolRegistry 执行底层 Tool；普通异常由 Registry 包成 `TOOL_EXECUTION`。
6. PostToolUse hook 只观察；异常追加 diagnostic，不覆盖 ToolResult。
7. Agent 把 ToolResult 转 Observation，然后继续 Reflection / 下一轮。

主要不一致：

- direct Runner 默认没有注入 PermissionManager，而 isolate orchestrator 有 workspace-bound PermissionManager；因此四入口在 direct/isolate 路径上并非同一 permission contract。
- ToolExecutor 内没有 pre-hook 后、permission 前后、tool 前等 cancel 安全边界。
- permission / confirm 子系统异常没有稳定分类：permission.check 会冒泡，confirm callback 异常会被误当成普通用户拒绝。
- 同步 Tool 返回后没有立即 cancel 边界，可能继续写 history / loop / reflection 后才在下一 step 停止。
- `TIMEOUT` error_type 最终仍被转换成普通 `ObservationStatus.ERROR`。

## 最终执行链

```text
Run / Step
  │
  ├─ cooperative cancel check
  ├─ model call
  ├─ cooperative cancel check after model return
  │
  ├─ TOOL_EXECUTION_STARTED
  ├─ cooperative cancel check
  ├─ validate tool call
  │    ├─ UNKNOWN_TOOL
  │    └─ INVALID_ARGUMENTS
  │
  ├─ pre-hook
  │    ├─ pass
  │    ├─ block  -> HOOK_BLOCKED Observation
  │    └─ failure -> HOOK_FAILED Observation
  │
  ├─ cooperative cancel check
  ├─ permission
  │    ├─ allow
  │    ├─ deny/confirm reject -> PERMISSION_DENIED Observation
  │    └─ subsystem failure -> infrastructure_error / Run FAILED
  │
  ├─ cooperative cancel check
  ├─ tool execution
  │    ├─ success
  │    ├─ timeout -> TIMEOUT Observation
  │    ├─ normal failure -> TOOL_EXECUTION Observation
  │    └─ runtime infrastructure -> existing fatal-infrastructure policy
  │
  ├─ post-hook
  │    ├─ success
  │    └─ exception -> diagnostic only; real ToolResult preserved
  │
  ├─ Observation + Tool Trace
  ├─ cooperative cancel check after Tool/post-hook
  │
  └─ history / Reflection / prepare_next_turn / next model call
```

## Hook 语义

### Pre-hook block

- Tool 不执行。
- Permission 不执行；pre-hook 是 permission 之前的策略门。
- Post-hook 不执行，因为底层 Tool 从未发生。
- 返回稳定失败 ToolResult：`error_type=HOOK_BLOCKED`。
- Observation 为 `ERROR`。
- Agent 可看到 Observation 并在后续 step Reflection / 改参 / 换工具。
- Run 不因单次 hook block 自动 FAILED。

### Pre-hook exception

保持既有设计意图：hook callback 是可配置策略扩展，异常按 fail-closed 处理当前 Tool 调用，但不直接摧毁整个 Agent Run。

- Tool / Permission / post-hook 都不执行。
- 返回 `HOOK_FAILED` Observation。
- Agent 可恢复。
- 如果异常发生同时外部 cancel 已置位，则 cancel 优先，在该安全边界进入 CANCELED。

### Post-hook exception

Tool 已经发生后，post-hook 只能作为观察/诊断层：

- 不覆盖真实 ToolResult。
- 不把成功 Tool 伪装成失败或“未执行”。
- diagnostic 追加 `post_tool_hook:<ExceptionType>`。
- Agent 不会因为 post-hook diagnostic 自动重试有副作用的 Tool。

## Permission 语义

产品路径通过 `harness.ToolExecutor` 获得默认 PermissionManager；低层透明 executor 仍可从 `harness.executor.ToolExecutor` 使用，便于内部测试和显式组合。

- ALLOW：继续执行 Tool。
- DENY：返回 `PERMISSION_DENIED` Observation；Run 可继续。
- CONFIRM + 用户拒绝 / 无 callback：属于正常策略拒绝，同样为 `PERMISSION_DENIED`。
- `PermissionManager.check` 自身抛异常：不是 deny，属于 framework infrastructure failure，Run 立即 `FAILED`，`termination_reason=infrastructure_error`。
- confirm callback 自身抛异常：同样是 permission subsystem failure，而不是“用户拒绝”。
- permission decision Trace observer 本身属于 best-effort observability；observer 写日志失败不改变 permission 结果。

Direct 模式继续依赖 workspace-aware file tools 做文件路径边界；isolate 额外使用 `PermissionManager(workspace=<worktree>)`，因此不会削弱原 worktree 边界。

GitHub Issue 自动 PR 模式仍不向 Agent 重新开放 `git_add` / `git_commit`。

## Tool failure 分类

| 场景 | Observation | error_type | Agent 可继续 | Run 立即终止 |
| --- | --- | --- | --- | --- |
| unknown tool | ERROR | `unknown_tool` | 是 | 否 |
| invalid arguments | ERROR | `invalid_arguments` | 是 | 否 |
| pre-hook block | ERROR | `hook_blocked` | 是 | 否 |
| pre-hook exception | ERROR | `hook_failed` | 是 | 否 |
| permission deny / confirm reject | ERROR | `permission_denied` | 是 | 否 |
| normal tool failure | ERROR | `tool_execution` | 是 | 否 |
| timeout | TIMEOUT | `timeout` | 是 | 否 |
| runtime infrastructure ToolResult | ERROR | `infrastructure` | 受既有 fatal detector 控制 | 不是所有首个错误立即终止 |
| permission / confirm subsystem crash | 无普通 Observation | `infrastructure`（tool span） | 否 | 是 |
| ToolExecutor framework crash | 无普通 Observation | `infrastructure`（tool span） | 否 | 是 |
| post-hook exception | 保留真实 Tool Observation | real Tool error_type | 按真实 ToolResult | 否 |

既有 fatal infrastructure detector 未改造成新的 Resource Manager：已知 Docker/runtime 故障仍由现有重复错误和 completion guard 语义处理；普通 Tool failure 不会被升级为 Run FAILED。

## Cancel 语义

本轮冻结的是 cooperative cancellation，不声称能强杀任意同步函数。

安全边界包括：

- Run / step 开始前；
- model call 前的 retry 边界，以及同步 model call 返回后的第一安全边界；
- Tool validation / pre-hook 前；
- pre-hook 完成后；
- permission 前、permission/confirm 完成后；
- Tool 真正执行前；
- Tool + post-hook 完成、Observation 已落盘后；
- `prepare_next_turn` 前后。

如果 cancel 在 Tool 尚未开始时到达：Tool 不执行，Run 进入：

```text
RunStatus.CANCELED
termination_reason = "canceled"
```

如果同步 Tool 已经开始：

- 不尝试强制中断该同步调用；
- Tool 返回后仍执行 post-hook，以完成该次已发生副作用的调用的诊断边界；
- 真实 ToolResult、diagnostics 和 Observation 先写 Trace；
- 下一安全边界立即返回 CANCELED，不再进入 history / Reflection / 下一次 model call。

如果 cancel 在 post-hook 内发生，语义相同：真实 ToolResult 保留，post-hook 完成后进入 CANCELED。

同步 provider call 和同步 `prepare_next_turn` callback 同样不能被本轮强杀；只能在返回后的第一安全边界停止。LLM retry wait 若 cancel_event 提供 `wait()`，现在可以在等待期间协作唤醒。

## Trace v2 对齐

没有新增大量 Hook lifecycle event。继续复用 P0-3 已冻结 schema：

- permission 走既有 `permission_decision`；
- tool 走既有 `tool_execution_started/finished/failed`；
- cancel / fatal lifecycle 最终走既有 `run_terminated`；
- Tool span 继续携带 `run_id`、`run_span_id`、`step_id`、`tool_execution_id`、`span_id`、`parent_span_id`；
- recoverable failures 的 `error_type` 使用现有 ToolErrorType 值；
- permission/confirm/framework crash 在 Tool failed span 中记录 `error_type=infrastructure`，并补最小 `lifecycle_phase` / `framework_error_type` 诊断字段；
- Tool 已发生后收到 cancel 时，Tool finished/failed span 保留真实结果，并可见 `cancel_requested_after_tool` diagnostic；最终 run termination 为 canceled；
- 所有新增 payload 仍通过 EventLog 的 Trace v2 写盘级 schema redaction，没有另造脱敏层。

## 四入口一致性

- CLI direct：ExecutionRunner → 生产 ToolExecutor → Agent。
- CLI isolate：ExecutionRunner → orchestrate_run → worktree PermissionManager → raw ToolExecutor → Agent。
- Chat：每轮复用 ExecutionRunner / Agent；同一 Tool lifecycle。
- API：`threading.Event` 从 RunRequest 进入 AgentConfig，并在 isolate orchestrator 内继续传播到 Agent / ToolExecutor。
- GitHub Issue：ExecutionRunner direct；Tool lifecycle 与其它入口一致，自动 PR 模式的工具集权限边界保持不变。

没有入口直接绕过统一 Agent ToolExecutor 执行 Tool。

## 修改文件

生产代码：

- `harness/executor.py`
- `harness/__init__.py`
- `tools/base.py`
- `agent/core.py`

测试：

- `tests/test_tool_lifecycle_p0_2.py`

状态与文档：

- `TODO-P0-P1.md`
- `Forge-Agent-P0-P1-实施计划.md`
- `AGENTS.md`
- `docs/README.md`
- `docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`

未修改：`config/default.yaml`、B1/B2 fixture、`evals/results` 历史结果。

## Tests

新增定向覆盖：

- 正常 pre-hook → permission → tool → post-hook 顺序；
- pre-hook block / exception；
- permission allow / deny / subsystem exception / confirm exception；
- unknown tool / invalid arguments；
- normal Tool failure / timeout / runtime infrastructure；
- post-hook exception 不覆盖 ToolResult；
- cancel after pre-hook / after permission / during Tool / during post-hook / after model / during prepare；
- canceled Run Trace；
- permission / hook / tool error Trace；
- CLI / Chat / API / GitHub Issue entrypoint 参数化一致性；
- direct / isolate permission denial 一致性；
- Trace redaction 对 permission infrastructure error 的回归。

本轮 GitHub connector 只有仓库读写/提交能力，没有仓库执行环境，因此 pytest 未在远程会话中真实运行。测试状态必须保持为“测试代码已补齐，待本地执行”，不能宣称通过。

用户 pull 后建议：

```bash
pytest tests/test_tool_lifecycle_p0_2.py tests/test_harness.py tests/test_runner.py tests/test_confirm.py -q

pytest tests/test_trace_v2.py tests/test_agent_completion_guards.py \
  tests/test_chat.py tests/test_api.py tests/test_cli_isolate.py \
  tests/test_github_issue_delivery.py -q

pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q

pytest -q
```

## 已知限制

- 只实现 cooperative cancellation；不强杀已进入执行的同步 Tool、同步 provider call 或同步 prepare callback。
- 没有重写 shell subprocess 为通用可取消 Resource Manager；ShellTool 自己已有的 timeout 仍按原实现工作。
- 没有为 Hook 新造事件体系；pre-hook 结果通过 Tool error_type，post-hook 异常通过 diagnostics 表达。
- P1-4 failure Harness 尚未开始；本轮新增测试只用于 P0-2 生命周期验收，不是新的 benchmark。
- P0-3 schema、B1/B2 reader、historical results 保持不变。

## 下一步

P0-2 的代码门槛已满足，可标记 DONE；本地 pytest 若发现回归则重新打开 P0-2。

下一轮从 P1-4 开始：先冻结一个默认离线、deterministic 的 failure injection matrix，覆盖 provider timeout/空响应、permission failure、hook failure、cancel 和 infrastructure failure 的期望 RunStatus / termination / acceptance / delivery，再实现统一 Harness 入口。不要在本轮提前开始 P1-4。
