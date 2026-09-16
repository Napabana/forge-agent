# Forge Agent P0/P1 当前状态

> 状态基线：2026-09-16。P0-2 本轮以 `dev@adb1252884743804610923f9c27a9849a4f9ba4b` 为实现基线，
> Tool Hook / Permission / Cancel 生产语义已收口；本文件只保留当前结论和剩余工作。历史过程见 [`docs/README.md`](docs/README.md)。

状态定义：`DONE` 已有代码与对应回归覆盖；`PARTIAL` 核心能力存在但仍有明确缺口；
`TODO` 尚未实施；`DEFERRED` 已有意延后，不属于当前主线验收。

> 测试说明：P0-2 回归测试代码已补齐，但当前 GitHub connector 无仓库执行环境，pytest 尚未由本轮远程会话实际运行；
> 用户 pull 后执行文末/变更日志中的命令做最终本地验证。若发现回归，P0-2 应立即重新打开而不是隐藏失败。

## 状态总览

| 项目 | 状态 | 当前结论 |
| --- | --- | --- |
| P0-1 `prepare_next_turn` / shared-history 边界 | DONE | 策略插槽、首轮共享历史 preflight 和多轮接线已落地 |
| P0-2 Tool Hook / Permission / Cancel 生产语义 | DONE | validate→pre-hook→permission→tool→post-hook、错误分类、cooperative cancel、四入口/direct-isolate 契约已冻结 |
| P0-3 Trace v2 最小闭环 | DONE | v2 schema/correlation、写盘级脱敏、prompt token 分区、termination/acceptance/delivery 已统一；旧 JSONL 只读兼容 |
| P1-1 统一 Runner 与独立验收 | DONE | Runner、acceptance、交付门禁及真实 PR 闭环均有证据 |
| P1-2 Context Compaction | DONE | C1-C5、B1 与 B2 均已完成；C6 单独延期 |
| P1-3 Session 加固 | DONE | 并发占用、恢复与共享历史边界已有实现和测试 |
| P1-4 固定 Harness 任务集 | PARTIAL | 编码/上下文 fixture 和真实 Agent 样本已有，统一失败注入矩阵仍待实施 |
| P1-5 Repo Map 核心能力 | DONE | query-aware 排序、同 run 写后刷新和正式消融已完成 |
| P1-6 面试证据包 | PARTIAL | 真实 PR、Trace、报告齐备，统一可复现入口与叙事仍可精简 |

## P0-1：`prepare_next_turn` 与共享历史边界 — DONE

- [x] `prepare_next_turn` 是显式策略插槽，不把压缩逻辑硬编码进入口。
- [x] fresh run 与 shared-history run 分流；已有共享历史在第一次模型调用前执行同一 Context Policy preflight。
- [x] Agent 内后续 step 继续复用相同生命周期。
- [x] canonical History 与 model-facing view 分离，压缩不破坏审计历史。
- [x] Chat/Session/Runner 回归覆盖共享历史传递和 round preflight。

证据：`agent/runner.py`、`context/history.py`、`tests/test_chat.py`、`tests/test_compaction.py`。

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
- [x] 新增集中回归 `tests/test_tool_lifecycle_p0_2.py`，覆盖四入口 entrypoint、direct/isolate、error classification、cancel 边界和 redaction 回归。

已知边界：

- cooperative cancellation 不等于强制终止正在执行的同步 Tool、同步 provider call 或同步 `prepare_next_turn` callback；只能在返回后的安全边界停止。
- direct 文件路径边界继续由 workspace-aware file tools 保证；isolate 额外有 `PermissionManager(workspace=<worktree>)`。
- 低层 `harness.executor.ToolExecutor` 仍支持显式无 PermissionManager 的透明组合；产品组合根使用 `harness.ToolExecutor` 的安全默认。
- 本轮没有实现 Resource Manager、async Tool framework、P1-4 failure Harness 或新 benchmark。

证据：`harness/executor.py`、`harness/permission.py`、`harness/__init__.py`、`tools/base.py`、`agent/core.py`、
`tests/test_tool_lifecycle_p0_2.py`、`tests/test_harness.py`、`tests/test_trace_v2.py`、
[`docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`](docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md)。

## P0-3：Trace v2 最小闭环 — DONE

- [x] 新写入事件统一携带 `trace_schema_version=2`，保留 `schema_version=2` 兼容别名。
- [x] Run 使用稳定 `run_id` / `run_span_id`；Model、Tool、Context/Compaction 使用 child span，包含 `step_id`、`span_id`、`parent_span_id` 与 operation-specific id。
- [x] CLI、Chat、API、GitHub Issue 通过统一 `ExecutionRunner` 解析/传播 `entrypoint`；isolate 路径使用轻量 `ContextVar` 传播。
- [x] EventLog 最终 JSONL 写盘边界统一递归 redaction；usage token 字段白名单保护。
- [x] Model span 分离本地 `token_breakdown` 与 provider `provider_usage`。
- [x] provider error、infrastructure error、cancel、loop detected、resource exhausted、completion rejection、acceptance、delivery 均可落到统一 run correlation 下。
- [x] Runner 统一追加 `acceptance` 与 `run_terminated`；GitHub Issue 在真实 commit/push/PR 决策后追加 delivery。
- [x] 旧 JSONL 读取/追加保持兼容，历史 B1/B2 reader 与 `evals/results` 未改写。

证据：`agent/trace_v2.py`、`agent/event_log.py`、`agent/core.py`、`agent/runner.py`、`entry/github_issue.py`、
`tests/test_trace_v2.py`、`tests/test_github_issue_delivery.py`、
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
- C6 `context_recall(event_ref)` 保持 `DEFERRED`。

证据：`evals/results/context_policy_benchmark/`、`evals/results/context_policy_agent_ablation_v3/`。

## P1-3：Session 加固 — DONE

- [x] Session 状态持久化、恢复和共享历史增长有回归测试。
- [x] 双进程/并发占用边界已收口。
- [x] shared-history 首轮 preflight 与 Context Policy 接线完成。

## P1-4：固定 Harness 任务集 — PARTIAL

已有：固定 context-policy fixture、真实 Agent ablation、仓库外 verifier、真实 PR 证据，以及 P0-2 的 Tool lifecycle 单元/集成回归。

下一批仍需：

- [ ] 冻结 deterministic failure injection matrix：provider timeout/空响应、permission subsystem failure、hook failure、cancel、runtime infrastructure 等。
- [ ] 为每类 failure 固定 RunStatus、termination reason、acceptance/delivery 期望。
- [ ] 提供一条默认离线、不调用付费模型的日常 failure Harness 回归入口。
- [ ] 真实模型实验继续显式 opt-in，且先冻结协议。

P0-2 的生命周期测试是 P1-4 的前置契约，不等于已经完成 P1-4。

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

1. P1-4：冻结并实现默认离线的 failure injection Harness。
2. P1-6：整理可重复、不过度宣称的面试证据入口。
3. P1-5 小尾项：仅在最小测试能稳定复现 cache / shell/git stale-map 问题时处理。

## P0-2 本地验证

```bash
pytest tests/test_tool_lifecycle_p0_2.py tests/test_harness.py \
  tests/test_runner.py tests/test_confirm.py -q

pytest tests/test_trace_v2.py tests/test_agent_completion_guards.py \
  tests/test_chat.py tests/test_api.py tests/test_cli_isolate.py \
  tests/test_github_issue_delivery.py -q

pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q

pytest -q
```

若本地测试失败，保留真实失败输出并重新打开对应 P0-2/P0-3 项；禁止通过修改 B1/B2 fixture 或历史 `evals/results` 规避回归。
