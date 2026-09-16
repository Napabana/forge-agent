# P1-4 Failure Harness / Deterministic Failure Injection 收口改动内容

日期：2026-09-16  
分支：`dev`  
P0-2 冻结基线：`1b567f5630c5176d09d274ecd1499f20b32612ef`  
P1-4 代码实现基线：`aeac887b61d4e463dd47e2f4d270ce0f547072f3`  
最终交接：以本文档提交后的 `dev` 最新 HEAD 为准。

## 1. 本轮目标

P1-4 不增加新的 Agent 能力，只把 Forge Agent 已经存在的失败语义固定成默认离线、deterministic、可重复、可断言、可由 Trace v2 审计的 regression harness。

本轮遵守以下边界：

- 不调用 OpenAI、Anthropic、DeepSeek 或任何在线 OpenAI-compatible provider；
- 不依赖 API Key、网络、真实 Docker 或真实 GitHub delivery；
- 不重新实现 Agent loop；
- 不修改 P0-2 生命周期方向；
- 不修改 Trace v2 schema；
- 不修改 `config/default.yaml`；
- 不修改 B1/B2 fixture；
- 不重写 `evals/results`；
- 不提前实现 P1-6、Resource Manager、多 Agent、MCP、async Tool framework 等范围外功能。

## 2. 原 failure testing 状态

收口前项目已有很多 failure building block，但分散在不同层：

- `llm/base.py::MockBackend` 可脚本化正常 Action，但不能直接表达 exception → exception → success 的 provider 序列；
- `llm/errors.py` 已有 provider error taxonomy 与 retryable 判定；
- `harness/executor.py` 已冻结 validate → pre-hook → permission → tool → post-hook；
- `harness/hooks.py`、`harness/permission.py` 已可注入 Hook/Permission 行为；
- `ToolRegistry` / fake Tool 已能表达 unknown/invalid/normal failure/exception；
- `threading.Event` / Event-like 对象已经是正式 cooperative cancel 注入点；
- `Agent` 已有 provider retry、fatal runtime detector、prepare failure、completion guard、loop/max-steps/GAVE_UP/cancel taxonomy；
- Trace v2 已能记录 provider/tool/context/termination/acceptance/delivery；
- P0-2 已有 Tool lifecycle/cancel 回归；
- Context semantic side-call failure 已有 structured fallback 回归；
- GitHub delivery 已有 fake/monkeypatch 测试。

缺口不是“没有失败处理”，而是没有一套统一、默认离线的 failure matrix，把 Provider/Hook/Permission/Tool/Cancel/Context/termination/acceptance/delivery 放在同一 production lifecycle 下验证。

## 3. Harness 最终设计

P1-4 选择 `ExecutionRunner` 作为 composition root：

```text
failure fixture / scripted fake
        ↓
ExecutionRunner
        ↓
Agent
        ↓
ToolExecutor
        ↓
EventLog / Trace v2
        ↓
RunResult / acceptance / delivery gate
```

fake 只负责注入失败，不实现第二套 Agent loop。

没有新增 production `FailureScenario` dataclass 或 failure framework。原因：

- 当前 pytest 参数化 + test-local helper 已足够把 scenario setup 与 assertion 分开；
- production 新 abstraction 会扩大 API 面，但不会增加 failure semantics 的真实性；
- `evals/harness.py` 面向固定任务/materialization/verification，不适合作为 production failure lifecycle composition root；
- `harness/executor.py` 是被测 Tool lifecycle，不应被包装成另一个简化 Agent loop。

因此最终结构是：

- `tests/test_failure_harness.py`：主要 failure matrix；
- `tests/test_failure_harness_isolate.py`：需要 orchestrator/isolate composition 的 sandbox preflight case；
- P0-2/P0-3/B2/Context/GitHub delivery 既有测试继续作为邻接回归，不复制全部已有 case。

## 4. Provider injection

新增 test-local `ScriptedFailureBackend`：

- 固定脚本可以逐次返回 `Action` / `LLMResponse`；
- 也可以逐次抛异常；
- 记录 `call_count` 与 received messages；
- 不访问网络；
- 不依赖 API Key；
- 不调用付费模型。

固定场景：

| 场景 | 注入 | 期望 |
| --- | --- | --- |
| retryable → success | `ConnectionError` → FINISH | 发生一次 `llm_call_retry`；第二次成功；最终 SUCCESS |
| timeout exhausted | `TimeoutError` × total attempts | `llm_call_failed(error_type=timeout)`；`FAILED/provider_error` |
| non-retryable | RuntimeError/unknown | 不 retry；`FAILED/provider_error` |
| parser failure | provider parser `ValueError` | 不 retry；`FAILED/provider_error` |
| retry wait cancel | Event-like `.wait()` 设置 cancel | 不进行下一次 provider call；`CANCELED/canceled` |

`llm/errors.py` 的稳定 taxonomy 继续原样使用，例如 `ConnectionError` 的 `error_type=connection`。

### malformed / empty response 边界

当前 `LLMBackend.complete()` 契约要求返回 `LLMResponse`。因此 P1-4 不扩展 Backend API 去允许 arbitrary bad object / `None`；malformed/empty provider payload 应在 provider adapter/parser 层转成 exception，Harness 通过 parser exception 固定为现有 `provider_error` 语义。

## 5. Hook / Permission / Tool injection

### Hook

- pre-hook block：`ObservationStatus.ERROR` + `error_type=hook_blocked`；Permission/Tool 不执行；Agent 可恢复。
- pre-hook exception：`ERROR/hook_failed`；Agent 可恢复。
- post-hook exception：Tool 已执行；原 ToolResult/Observation 不被覆盖；只追加 diagnostic；不会因为观察 hook crash 错误重试有副作用 Tool。

### Permission

- deny：`ERROR/permission_denied`；Agent 可恢复。
- confirm reject：与 deny 同一 Observation 分类；Trace 仍保留 `decision=confirm`。
- permission subsystem exception：`FAILED/infrastructure_error`。
- confirm callback exception：`FAILED/infrastructure_error`。

### Tool / Runtime

- unknown tool：`ERROR/unknown_tool`，可恢复。
- invalid arguments：`ERROR/invalid_arguments`，可恢复。
- normal `ToolResult(success=False)`：归一 `ERROR/tool_execution`，不直接升级 Run FAILED。
- Tool `execute()` exception：由 `ToolRegistry` 转换为 `tool_execution`，Agent 可继续。
- timeout：`ObservationStatus.TIMEOUT` + `error_type=timeout`。
- known runtime infrastructure：使用 deterministic Docker-marker error result 注入，不依赖真实 Docker。
  - 第一次 fatal runtime 可让 Agent 尝试恢复；
  - unresolved fatal 后 FINISH → `FAILED/infrastructure_error`；
  - 同一 fatal 重复到阈值 → 提前 `FAILED/infrastructure_error`。

没有重写现有 fatal detector。

## 6. Cancel injection

P1-4 继续使用 P0-2 的 cooperative cancellation，不实现强制 kill。

注入方式：

- `threading.Event`；
- retry wait 专用 Event-like fake；
- Hook/Permission/Tool/prepare callback 内设置 Event。

P1-4 + P0-2 回归共同覆盖：

- before run / before model；
- provider retry wait；
- pre-hook 后；
- permission 后；
- Tool 前；
- 同步 Tool 执行期间设置 cancel；
- Tool 返回后；
- post-hook 边界；
- prepare_next_turn 前、callback 内、返回后。

冻结语义：

```text
RunStatus.CANCELED
termination_reason=canceled
```

同步 provider / Tool / prepare callback 一旦开始不伪装成被中断；真实调用返回、真实副作用和 Tool outcome 保留，在下一安全边界停止。

## 7. Context / prepare failure

### step > 1

既有 `Agent._prepare_next_turn` 已固定：

- callback exception → `PREPARE_NEXT_TURN_FAILED` + `FAILED/infrastructure_error`；
- callback 内设置 cancel → callback 返回后 `CANCELED/canceled`；
- 不进行下一次 provider call。

### shared-history 首轮

审计发现 P1-4 前 `ExecutionRunner._prepare_shared_history_boundary` 直接调用 callback，导致首轮共享历史 prepare exception/cancel 没有复用 Agent 的 Trace/termination 语义。

本轮最小修正：

- 保留 Runner 首轮 shared-history composition；
- repo-map/context 初始化后直接调用 `Agent._prepare_next_turn(step=1, ...)`；
- 返回的 FAILED/CANCELED `RunResult` 直接走 Runner 的 acceptance/termination Trace；
- 不新增第二套 prepare helper。

结果：shared-history 首轮与 step>1 统一使用同一 production prepare lifecycle。

### semantic compaction side-call

`tests/test_structured_compaction.py` 已有 semantic side-call failure → `CONTEXT_COMPACTION_FAILED` → `structured-fallback-v1` 回归。本轮只把它纳入 P1-4 evidence，不重新打开 P1-2。

## 8. Completion / termination

Failure Harness 固定/复用以下分类：

| 场景 | RunStatus | termination_reason |
| --- | --- | --- |
| normal completion | SUCCESS | `completion_satisfied` |
| completion guard rejected then recover | 最终按后续结果 | rejection 不是立即 terminal |
| max steps | INCOMPLETE | `resource_exhausted` |
| loop detector | INCOMPLETE | `loop_detected` |
| model GIVE_UP | GAVE_UP | `agent_gave_up` |
| provider failure | FAILED | `provider_error` |
| permission/framework/fatal runtime/prepare infra | FAILED | `infrastructure_error` |
| cooperative cancel | CANCELED | `canceled` |

没有重新实现 B2 termination 逻辑。

## 9. Acceptance / delivery

### Agent 非成功

对于 FAILED / CANCELED / INCOMPLETE / GAVE_UP：

- independent acceptance verifier 不执行；
- `acceptance_status=skipped`；
- Trace `acceptance(status=skipped)`。

### Agent SUCCESS + acceptance failure

保持两层状态：

```text
Agent RunStatus.SUCCESS
termination_reason=completion_satisfied
acceptance_status=failed
delivery blocked
```

不把 acceptance failure 改成 Agent FAILED。

### GitHub delivery

P1-4 failure case 不进行真实 commit/push/PR。

新增回归通过 monkeypatch/fake dependency 明确断言 acceptance fail 时在 `_run_git` / PR creator 前返回 `blocked_acceptance`。既有 `tests/test_github_issue_delivery.py` 继续覆盖 agent failure 和其他 delivery 边界。

## 10. Isolate / runtime preflight

新增 `tests/test_failure_harness_isolate.py`：

- FakeWorktreeSession；
- FakeDockerRuntime；
- deterministic `preflight()` 返回“Docker is not available”；
- 不触真实 Docker；
- 不触 provider；
- registry construction 也不应开始；
- runtime cleanup 仍执行；
- `ExecutionRunner` 对该 pre-Agent isolate failure 补齐现有 taxonomy：`FAILED/infrastructure_error`；
- Runner append 的 `run_terminated` 同样携带该 termination reason。

本轮没有重写 `agent/orchestrate.py`。曾发现直接替换该 CRLF 文件会制造整文件 line-ending diff，因此恢复原 blob，最终只在选定的 `ExecutionRunner` composition root 做最小归一。

## 11. Trace v2

不增加 Trace v3。

P1-4 至少断言：

- provider：`llm_call_retry` / `llm_call_finished` / `llm_call_failed`；
- permission：`permission_decision`；
- Tool：`tool_execution_started` + finished/failed；
- prepare：started/finished/failed；
- cancel：保留已经发生的 operation event，再 `run_terminated(status=canceled)`；
- termination：统一 `run_terminated`；
- correlation：`run_id` / `run_span_id` / `model_call_id=span_id`；
- entrypoint：CLI/Chat/API/GitHub Issue 使用相同 Runner taxonomy；
- redaction：failure payload 中 `Authorization: Bearer <secret>` 不得出现在 JSONL 或 replay payload。

所有 failure payload 继续经过 EventLog 写盘级 redaction。

## 12. Scenario matrix

| 大类 | 代表场景 | deterministic 注入 | 主要断言 |
| --- | --- | --- | --- |
| Provider | retry → success | scripted exception/action | retry count、LLM Trace、SUCCESS |
| Provider | exhausted/non-retryable/parser | scripted exception | `FAILED/provider_error` |
| Provider | retry wait cancel | Event-like fake | 单次 provider call、CANCELED |
| Hook | block/pre exception/post exception | Hooks callback | error_type、side effect、diagnostic |
| Permission | deny/confirm/crash | FixedPermission/callback | recoverable vs infrastructure |
| Tool | unknown/invalid/fail/raise/timeout | fake Tool/registry | Observation/error_type/status |
| Runtime | unresolved/repeated fatal | Docker-marker ToolResult | fatal detector/termination |
| Cancel | lifecycle safe boundaries | `threading.Event` | no fake interruption、side effect preserved |
| Context | prepare fail/cancel | callback | prepare Trace + FAILED/CANCELED |
| Completion | guard/max/loop/GIVE_UP | scripted Actions | terminal taxonomy |
| Acceptance | non-success / verifier fail | fake verifier | skipped vs failed two-layer state |
| Delivery | acceptance block | fake git/PR dependency | no side effect before block |
| Trace | provider sensitive failure | secret-bearing exception | correlation + redaction |
| Isolate | sandbox preflight | fake runtime/worktree | no Docker/provider；FAILED/infrastructure |

## 13. 修改文件

生产代码：

- `agent/runner.py`

新增测试：

- `tests/test_failure_harness.py`
- `tests/test_failure_harness_isolate.py`

状态/交接：

- `TODO-P0-P1.md`
- `Forge-Agent-P0-P1-实施计划.md`
- `AGENTS.md`
- `docs/changes/2026-09-16/P1-4-Failure-Harness收口改动内容.md`

未修改：

- `config/default.yaml`
- B1 fixture
- B2 fixture
- `evals/results` 历史输出

## 14. 测试

推荐的默认离线 regression：

```bash
pytest tests/test_failure_harness*.py -q
```

然后：

```bash
pytest tests/test_tool_lifecycle_p0_2.py \
  tests/test_harness.py \
  tests/test_runner.py \
  tests/test_trace_v2.py \
  tests/test_agent_completion_guards.py \
  tests/test_compaction.py \
  tests/test_chat.py \
  tests/test_api.py \
  tests/test_github_issue_delivery.py -q
```

然后：

```bash
pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q
```

最后：

```bash
pytest -q
```

### 本轮真实执行状态

**未执行 pytest。**

原因：

- GitHub connector 只能读写仓库，没有代码执行环境；
- 当前容器尝试 `git clone --depth 1 --branch dev https://github.com/Napabana/forge-agent.git` 失败：`Could not resolve host: github.com`；
- 仓库当前没有可用 `.github/workflows` 可作为既有 CI 执行入口。

因此本文档只声明“测试代码已提交/静态审计完成”，不声明“测试通过”。用户本地 pull 后需要执行上述命令；任何失败都应 reopen P1-4。

## 15. 已知限制

- cooperative cancel 仍不能强杀任意同步 provider/Tool/prepare callback；这是 P0-2 冻结边界，不在 P1-4 扩 async runtime。
- malformed/empty raw provider payload 通过 parser exception 代表，因为 `LLMBackend` 对外契约是 `LLMResponse`。
- Failure Harness 是 deterministic regression，不是 benchmark，不产生“Agent 成功率”指标。
- 四入口的真实 wiring 继续由 `tests/test_tool_lifecycle_p0_2.py` / Chat/API/GitHub tests 覆盖；P1-4 不复制四套产品入口生命周期。
- isolate preflight 的 structured taxonomy 在 `ExecutionRunner` composition root 归一；未重构 orchestrator API。

## 16. 状态与下一步

P1-4 实现收口标记为 `DONE`，前提是用户本地最终 pytest 验证无回归；若本地 failure harness 或全量 pytest 失败，状态立即 reopen 为 PARTIAL。

下一批是 P1-6，起点不是写新功能，而是：

> 盘点 Forge Agent 当前已有的 implementation / deterministic regression / fixture benchmark / real-model sample / real-PR evidence，建立 evidence taxonomy，明确每条面试主张能证明什么、不能证明什么，再决定是否需要最小 evidence index/命令入口。

本轮不开始 P1-6。
