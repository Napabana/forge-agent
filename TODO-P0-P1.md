# Forge Agent P0/P1 当前状态

> 状态基线：2026-09-16。P1-4 Failure Harness 已在 `dev` 完成实现收口；代码实现基线为
> `aeac887b61d4e463dd47e2f4d270ce0f547072f3`，最终交接以当前 `dev` 最新 HEAD 为准。
> 历史过程见 [`docs/README.md`](docs/README.md)。

状态定义：`DONE` 已有代码与对应回归覆盖；`PARTIAL` 核心能力存在但仍有明确缺口；
`TODO` 尚未实施；`DEFERRED` 已有意延后，不属于当前主线验收。

> 测试说明：P1-4 新增 deterministic offline failure regression，但当前 GitHub connector 无仓库执行环境，
> 且容器无法解析 `github.com`，因此本轮远程会话没有真正执行 pytest。用户 pull 后必须按文末命令本地验证；
> 任一 P1-4/P0-2/P0-3 回归失败都应立即 reopen 对应条目，禁止把“测试代码已提交”写成“测试已通过”。

## 状态总览

| 项目 | 状态 | 当前结论 |
| --- | --- | --- |
| P0-1 `prepare_next_turn` / shared-history 边界 | DONE | 策略插槽、首轮共享历史 preflight 和多轮接线已落地 |
| P0-2 Tool Hook / Permission / Cancel 生产语义 | DONE | validate→pre-hook→permission→tool→post-hook、错误分类、cooperative cancel、四入口/direct-isolate 契约已冻结 |
| P0-3 Trace v2 最小闭环 | DONE | v2 schema/correlation、写盘级脱敏、prompt token 分区、termination/acceptance/delivery 已统一；旧 JSONL 只读兼容 |
| P1-1 统一 Runner 与独立验收 | DONE | Runner、acceptance、交付门禁及真实 PR 闭环均有证据 |
| P1-2 Context Compaction | DONE | C1-C5、B1 与 B2 均已完成；C6 单独延期 |
| P1-3 Session 加固 | DONE | 并发占用、恢复与共享历史边界已有实现和测试 |
| P1-4 Failure Harness / deterministic failure injection | DONE | Provider/Hook/Permission/Tool/Cancel/Context/termination/acceptance/delivery 已有默认离线 deterministic regression；无第二套 Agent loop |
| P1-5 Repo Map 核心能力 | DONE | query-aware 排序、同 run 写后刷新和正式消融已完成 |
| P1-6 面试证据包 | PARTIAL | 真实 PR、Trace、报告齐备，统一可复现入口与叙事仍可精简 |

## P0-1：`prepare_next_turn` 与共享历史边界 — DONE

- [x] `prepare_next_turn` 是显式策略插槽，不把压缩逻辑硬编码进入口。
- [x] fresh run 与 shared-history run 分流；已有共享历史在第一次模型调用前执行同一 Context Policy preflight。
- [x] Agent 内后续 step 继续复用相同生命周期。
- [x] canonical History 与 model-facing view 分离，压缩不破坏审计历史。
- [x] Chat/Session/Runner 回归覆盖共享历史传递和 round preflight。
- [x] P1-4 进一步让 shared-history 首轮 preflight 直接复用 `Agent._prepare_next_turn`，因此 exception/cancel/Trace 与 step>1 不再漂移。

证据：`agent/runner.py`、`context/history.py`、`tests/test_chat.py`、`tests/test_compaction.py`、`tests/test_failure_harness.py`。

## P0-2：Tool Hook / Permission / Cancel 生产语义 — DONE

冻结生命周期：

```text
cancel check
  → validate
  → pre-hook
  → cancel check
  → permission
  → cancel check
  → tool
  → post-hook
  → Observation / Trace
  → cancel check
  → history / Reflection / next turn
```

已完成：

- [x] validation 明确位于 Hook 之前；unknown tool / invalid arguments 不进入 Hook、Permission 或 Tool。
- [x] pre-hook block 固定为 `HOOK_BLOCKED`，Permission 与 Tool 均不执行，Agent 可恢复。
- [x] pre-hook callback exception 固定为 `HOOK_FAILED` fail-closed Observation，Agent 可恢复，不伪装成 infrastructure crash。
- [x] 产品 Runner 使用默认 PermissionManager；isolate 继续显式绑定 worktree PermissionManager；四产品入口均通过 Runner/Agent/ToolExecutor，不直接绕过中央执行链。
- [x] permission deny 与 confirm reject 固定为 `PERMISSION_DENIED` Observation，Agent 可恢复。
- [x] permission.check / confirm callback 自身异常与策略 deny 区分，固定为 framework infrastructure failure，Run `FAILED`，`termination_reason=infrastructure_error`。
- [x] normal Tool failure 固定为 `TOOL_EXECUTION`，不自动升级 Run FAILED；已知 runtime infrastructure 继续复用现有 fatal detector / completion guard。
- [x] `TIMEOUT` 保留 `ToolErrorType.TIMEOUT` 并映射到 `ObservationStatus.TIMEOUT`。
- [x] post-hook exception 只追加 diagnostic，不覆盖已发生 Tool 的真实结果。
- [x] cooperative cancel 覆盖 step/model 返回、pre-hook 后、permission 前后、Tool 前、Tool/post-hook 后、prepare_next_turn 前后等安全边界。
- [x] 已开始执行的同步 Tool 不强杀；Tool + post-hook 完成并写入真实 Trace/Observation 后，在下一安全边界进入 `RunStatus.CANCELED` / `termination_reason=canceled`。
- [x] permission decision、tool span、run termination 全部复用 P0-3 Trace v2；没有新增大批 Hook event；新增诊断字段仍通过写盘级 redaction。
- [x] 集中回归 `tests/test_tool_lifecycle_p0_2.py` 与 P1-4 failure matrix 共同冻结上述语义。

已知边界：

- cooperative cancellation 不等于强制终止正在执行的同步 Tool、同步 provider call 或同步 `prepare_next_turn` callback；只能在返回后的安全边界停止。
- direct 文件路径边界继续由 workspace-aware file tools 保证；isolate 额外有 `PermissionManager(workspace=<worktree>)`。
- 低层 `harness.executor.ToolExecutor` 仍支持显式无 PermissionManager 的透明组合；产品组合根使用 `harness.ToolExecutor` 的安全默认。
- 本轮没有实现 Resource Manager、async Tool framework、新 Provider 或新 Agent 算法。

证据：`harness/executor.py`、`harness/permission.py`、`harness/__init__.py`、`tools/base.py`、`agent/core.py`、
`tests/test_tool_lifecycle_p0_2.py`、`tests/test_failure_harness.py`、`tests/test_harness.py`、`tests/test_trace_v2.py`、
[`docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`](docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md)。

## P0-3：Trace v2 最小闭环 — DONE

- [x] 新写入事件统一携带 `trace_schema_version=2`，保留 `schema_version=2` 兼容别名。
- [x] Run 使用稳定 `run_id` / `run_span_id`；Model、Tool、Context/Compaction 使用 child span，包含 `step_id`、`span_id`、`parent_span_id` 与 operation-specific id。
- [x] CLI、Chat、API、GitHub Issue 通过统一 `ExecutionRunner` 解析/传播 `entrypoint`；isolate 路径使用轻量 `ContextVar` 传播。
- [x] EventLog 最终 JSONL 写盘边界统一递归 redaction；usage token 字段白名单保护。
- [x] Model span 分离本地 `token_breakdown` 与 provider `provider_usage`。
- [x] provider error、infrastructure error、cancel、loop detected、resource exhausted、completion rejection、acceptance、delivery 均可落到统一 run correlation 下。
- [x] Runner 统一追加 `acceptance` 与 `run_terminated`；GitHub Issue 在真实 commit/push/PR 决策后追加 delivery。
- [x] P1-4 增加 failure-path correlation/redaction 回归，不新增 Trace v3 或新的 failure event family。
- [x] 旧 JSONL 读取/追加保持兼容，历史 B1/B2 reader 与 `evals/results` 未改写。

证据：`agent/trace_v2.py`、`agent/event_log.py`、`agent/core.py`、`agent/runner.py`、`entry/github_issue.py`、
`tests/test_trace_v2.py`、`tests/test_failure_harness.py`、`tests/test_github_issue_delivery.py`、
[`docs/changes/2026-09-16/Trace-v2收口改动内容.md`](docs/changes/2026-09-16/Trace-v2收口改动内容.md)。

## P1-1：统一 Runner、独立验收与真实 PR — DONE

- [x] `ExecutionRunner` 统一运行请求、Trace、结果和 acceptance contract。
- [x] hidden verifier 不进入 canonical History，只有独立验收通过才允许交付。
- [x] `--no-pr` 与自动 PR registry 的提交权边界有测试。
- [x] clone/push 使用临时认证 header，remote 和日志不保存 Token。
- [x] 真实案例完成 Agent → verifier → commit → push → PR → merge 闭环。

证据：`agent/runner.py`、`entry/github_issue.py`、`tests/test_runner.py`、`tests/test_github_issue_delivery.py`。

## P1-2：Context Compaction — DONE（C6 延期）

- C1-C5 已完成：canonical/model view 分离、HistoryUnit、预算与 fingerprint、tool pruning、structured/semantic compaction、usage/Trace/Runner 集成。
- B1 离线 benchmark：7 cases × 3 variants = 21 个 fixture 样本；hybrid 7/7，仅代表冻结 fixture。
- B2 termination 与 v3 已完成；v3 为 3 cases × 3 variants × 1 run，共 9 个真实 run，不能外推总体胜率。
- semantic side-call failure 已有 `CONTEXT_COMPACTION_FAILED → structured-fallback-v1` 回归；P1-4 只复用，不重构 Context Policy。
- C6 `context_recall(event_ref)` 保持 `DEFERRED`。

证据：`evals/results/context_policy_benchmark/`、`evals/results/context_policy_agent_ablation_v3/`、`tests/test_structured_compaction.py`。

## P1-3：Session 加固 — DONE

- [x] Session 状态持久化、恢复和共享历史增长有回归测试。
- [x] 双进程/并发占用边界已收口。
- [x] shared-history 首轮 preflight 与 Context Policy 接线完成。

## P1-4：Failure Harness / deterministic failure injection — DONE

组合根与原则：

```text
Failure fixture / scripted fake
        ↓
ExecutionRunner
        ↓
Agent
        ↓
ToolExecutor
        ↓
EventLog / Trace v2 / RunResult
```

没有新增第二套 Agent loop，也没有把 `evals/harness.py` 或 `harness/executor.py` 变成 failure-only 生命周期。

已完成：

- [x] 新增 test-local `ScriptedFailureBackend`，可按固定序列 return/raise，不调用 OpenAI、Anthropic、DeepSeek 或任何在线 provider。
- [x] Provider：connection/timeout retry、retry exhausted、non-retryable、parser exception、retry wait cancel；固定 `llm_call_retry/finished/failed` 与 `provider_error/canceled`。
- [x] Hook：pre-hook block=`hook_blocked`、pre-hook exception=`hook_failed`、post-hook exception 只追加 diagnostic，副作用 Tool 不被错误重试。
- [x] Permission：deny/confirm reject=`permission_denied`；permission subsystem/confirm callback crash=`FAILED/infrastructure_error`。
- [x] Tool/Runtime：unknown tool、invalid args、normal failure、execute exception、timeout、first unresolved fatal runtime、repeated fatal runtime 都有 deterministic injection。
- [x] Cancel：before run、provider retry wait、pre-hook 后、permission 后、同步 Tool 执行中、post-hook 边界，以及 prepare callback 前后行为由 P1-4 + P0-2 回归共同固定；已开始的同步调用不伪装成被中断。
- [x] Context：step>1 prepare exception/cancel 与 shared-history 首轮 prepare exception/cancel 统一；首轮现直接复用 `Agent._prepare_next_turn`。
- [x] Completion/termination：completion guard recover、max_steps=`INCOMPLETE/resource_exhausted`、loop=`INCOMPLETE/loop_detected`、GAVE_UP、fatal infrastructure、cancel、provider failure 有统一 regression。
- [x] Acceptance：FAILED/CANCELED/INCOMPLETE/GAVE_UP 时 verifier `skipped`；Agent SUCCESS + acceptance failure 保持两层状态。
- [x] Delivery：acceptance failure 在任何 git/push/PR side effect 前 blocked；既有 GitHub delivery 测试继续覆盖 Agent failure 与交付失败边界。
- [x] Trace：关键 failure event + `run_terminated`、run/model correlation、entrypoint、一条 Bearer sensitive-data failure path redaction 有回归。
- [x] isolate sandbox preflight 使用 fake DockerRuntime/FakeWorktreeSession，完全离线且不依赖真实 Docker；`ExecutionRunner` 将该 pre-Agent failure 归一为 `FAILED/infrastructure_error`。
- [x] 四入口通过同一 Runner taxonomy；P0-2 既有真实 wiring 测试负责 CLI/Chat/API/GitHub Issue 产品接线，P1-4 参数化固定相同 provider termination contract。
- [x] 默认日常入口：`pytest tests/test_failure_harness*.py -q`。
- [x] 未修改 `config/default.yaml`、B1/B2 fixture 或历史 `evals/results`。

边界：

- LLMBackend 契约要求返回 `LLMResponse`；所谓 malformed/empty response 在现有 abstraction 中以 provider parser exception 注入，不新增“返回任意坏对象”的正式 Backend 契约。
- isolate sandbox preflight 原始 orchestrator `RunResult` 仍是历史结构；产品 `ExecutionRunner` composition root 在 post-orchestrate 边界补齐既有 `infrastructure_error` taxonomy，没有重写 orchestrator 生命周期。
- 本轮没有新增 failure CLI/eval report，因为 pytest 已能表达 correctness source；避免维护第二套结果框架。
- 本轮远程 pytest **未执行**。若用户本地验证失败，P1-4 状态立即改回 PARTIAL。

证据：`agent/runner.py`、`tests/test_failure_harness.py`、`tests/test_failure_harness_isolate.py`、
`tests/test_tool_lifecycle_p0_2.py`、`tests/test_llm_retry_improvements.py`、`tests/test_prepare_next_turn.py`、
`tests/test_agent_completion_guards.py`、`tests/test_trace_v2.py`、`tests/test_github_issue_delivery.py`、`tests/test_structured_compaction.py`。

## P1-5：Repo Map 核心能力 — DONE（边界另列）

核心已完成：query-aware ranking、同 run 文件工具写后刷新、正式 retrieval 消融与引用计数性能优化。

正式结果：MRR 0.097 → 0.319；预算内目标召回 0.365 → 0.635；引用计数 median 35.118s → 0.493s（约 71.3×）。这些均只代表冻结实验协议。

边界：cache identity、shell/git 写入/删除/重命名感知、parser fallback 契约属于条件执行尾项，不影响本阶段 DONE。

## P1-6：自动 PR 与面试证据包 — PARTIAL

已完成真实 PR、独立 verifier、Trace、失败样本与 benchmark 报告。剩余工作只做证据产品化：

- [ ] 提供一个可重复执行、默认离线的 evidence index / 命令入口。
- [ ] 将实现事实、单样本案例、正式消融、仍未知拆成稳定面试表述。
- [ ] 不把一次真实 PR、`n=3` Agent ablation 或 fixture benchmark 外推为总体成功率。

## 明确延期或不在当前计划

- `DEFERRED`：完整 Resource Manager（总 token、wall-clock、成本硬预算）。
- `DEFERRED`：hidden-verifier feedback 回灌 Agent。
- `DEFERRED`：C6 `context_recall(event_ref)`，除非新 benchmark 证明必要。
- `DEFERRED`：MCP、通用 Skill 市场、多 Agent、多工具并行调用。
- `DEFERRED`：tree-structured session、自动 merge、无人监督发布。
- `DEFERRED`：为提升指标而静默重跑、调整 benchmark 或扩大付费调用范围。

## 下一批建议顺序

1. P1-6：整理可重复、不过度宣称的面试证据入口；不要提前把 P1-4 failure case 包装成 benchmark 成功率。
2. P1-5 小尾项：仅在最小测试能稳定复现 cache / shell/git stale-map 问题时处理。

## P1-4 本地验证

先跑默认离线 failure regression：

```bash
pytest tests/test_failure_harness*.py -q
```

然后跑生命周期 / Runner / Trace / 四入口回归：

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

再跑冻结 Context Policy reader：

```bash
pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q
```

最后：

```bash
pytest -q
```

本轮远程会话没有执行上述 pytest。若本地测试失败，保留真实失败输出并 reopen P1-4/P0-2/P0-3；禁止修改 B1/B2 fixture 或历史 `evals/results` 规避回归。
