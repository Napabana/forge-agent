# Forge Agent P0/P1 实施计划（当前版）

> 更新基线：2026-09-16。当前状态的唯一清单是 [`TODO-P0-P1.md`](TODO-P0-P1.md)；
> 本文只说明已完成主线、下一批实施顺序和明确延期项。

## 1. 当前结论

Forge Agent 当前已经完成这一阶段的三个 P0 生命周期基础项：

- P0-1：`prepare_next_turn` / shared-history 边界；
- P0-2：Tool Hook / Permission / Cancel 生产语义；
- P0-3：Trace v2 最小闭环。

同时已完成 P1-1 统一 Runner/独立验收、P1-2 Context Compaction、P1-3 Session 加固和 P1-5 Repo Map 核心能力。

P0-2 本轮冻结了中央 Tool lifecycle：

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

其中：

- unknown tool / invalid arguments、hook block/failure、permission deny、普通 Tool failure、timeout 均是可恢复 Tool Observation；
- permission/confirm 子系统 crash 和 ToolExecutor framework crash 属于 infrastructure failure，Run 立即 `FAILED`；
- post-hook failure 不覆盖已发生 Tool 的真实结果；
- cancel 为 cooperative cancellation，已开始的同步 Tool/provider/callback 不做强杀，只在下一安全边界停止；
- Tool 已经发生后收到 cancel 时，先保留 ToolResult、post-hook diagnostic 和 Observation，再进入 `CANCELED`；
- 四入口继续统一走 ExecutionRunner/Agent/ToolExecutor；GitHub Issue 自动 PR 不重新开放 `git_add/git_commit`。

Trace 仍使用 P0-3 既有 schema，没有为 P0-2 另造 tracing framework。P0-2 详细契约见：
[`docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`](docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md)。

> 验证状态：P0-2 回归测试代码已补齐，但当前 GitHub connector 无仓库执行环境；用户 pull 后在本地
> 运行定向测试与全量 pytest。若出现回归，重新打开 P0-2，不通过修改 fixture 或历史结果规避失败。

## 2. 已完成主线

### 2.1 生命周期、Runner 与 Trace

- `prepare_next_turn` 已作为策略插槽接入 Agent step 和 shared-history run 首轮 preflight。
- `ExecutionRunner` 统一 run、Trace、acceptance 和结果边界。
- Tool lifecycle 已统一 validation、Hook、Permission、Tool、post-hook、cancel 与 error classification。
- 产品 Runner 使用默认 PermissionManager；isolate 使用 workspace-bound PermissionManager。
- Trace v2 统一 schema/correlation/redaction/termination/acceptance/delivery。
- hidden verifier 与 canonical History 隔离；验收不通过不得进入 delivery。
- 自动 PR 已完成真实仓库的 Agent → verifier → commit → push → PR → merge 案例。

结论：P0-1、P0-2、P0-3、P1-1 已完成；不再重复设计新的 Runner、Hook framework 或 tracing framework。

### 2.2 Context Compaction

- C1-C5 已完成：canonical/model view 分离、HistoryUnit、预算与 fingerprint、tool pruning、structured/semantic compaction、usage/Trace/Runner 集成。
- B1 离线 benchmark 已完成 7×3=21 个 fixture 样本；hybrid 7/7，仅代表冻结任务集。
- B2 termination 已完成，v3 按冻结参数执行 3×3×1=9 个真实 run。
- C6 `context_recall(event_ref)` 延期，只有稳定 benchmark 出现明确需求时才重启。

结论：P1-2 已完成。

### 2.3 Repo Map

- query-aware ranking 使用 path、symbol 和 source text。
- 文件工具成功写入后失效缓存，同一 run 可重新构建。
- 正式 retrieval 消融：MRR 0.097 → 0.319，预算内目标召回 0.365 → 0.635。
- 引用计数优化：median 35.118s → 0.493s，约 71.3×，语义等价。

结论：P1-5 核心完成。cache identity、shell/git 写入感知等只在测试稳定复现时做尾项。

### 2.4 Session、Harness 与真实交付

- Session 恢复、共享历史和双进程占用边界已加固。
- P0-2 已新增 Tool lifecycle/cancel/error classification 的集中离线回归。
- 固定 context-policy fixture、真实 Agent ablation、仓库外 verifier 和真实 PR 证据已存在。

结论：P1-3 完成；P1-4、P1-6 仍保持 `PARTIAL`。

## 3. 下一批执行顺序

### Batch A：固定 Harness failure injection 任务集（P1-4）

目标：把 provider、permission、hook、cancel、runtime infrastructure 的失败边界变成默认离线、deterministic 的统一回归，而不是继续散落在单元测试中。

开始 P1-4 时先冻结，不先写功能：

1. failure case taxonomy：provider timeout/空响应/协议错误、permission subsystem crash、hook block/failure、cancel 各阶段、runtime infrastructure；
2. 每个 case 的固定期望：ToolResult/Observation、RunStatus、termination reason、acceptance、delivery、Trace；
3. deterministic fake provider / fake tool / fake hook 的注入接口；
4. 默认离线的一条 Harness 回归命令；真实 provider 实验保持显式 opt-in；
5. 与现有 ExecutionRunner / Trace v2 / P0-2 lifecycle 复用，禁止建立第二套生命周期。

验收重点：失败注入本身可重复、无需付费模型、不会修改 B1/B2 fixture，也不会把 P0-2 单元测试包装成虚假的 benchmark 指标。

### Batch B：证据包产品化（P1-6）

目标：把已经存在的实现、测试和实验整理成可复现、不过度宣称的面试证据。

1. 维护 evidence index。
2. 提供默认离线的复现命令。
3. 将实现事实、真实单案例、固定 fixture benchmark、小样本真实模型实验、仍未知分开。
4. 最终简历只引用能指向代码、测试或正式结果的主张。

### Batch C：Repo Map 小尾项（条件执行）

仅在最小测试能稳定复现时处理：

- cache identity 未包含 Git HEAD/working-tree fingerprint 导致错误复用；
- shell/git 写入、删除或重命名导致同 run map 陈旧；
- parser fallback 导致 Agent 把陈旧 map 当成强事实。

不为追求更漂亮指标追加付费实验。

## 4. P0-2 / P0-3 本地验证命令

用户 pull 后优先运行：

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

其中第一组负责 P0-2 生命周期定向回归；第二组验证 P0-3、Completion Guard 与四入口；第三组保证 B1/B2 reader 未受影响；最后一条做全量确认。

## 5. 证据基线

| 能力 | 证据 | 可安全表述 |
| --- | --- | --- |
| Tool lifecycle | `harness/executor.py`、`agent/core.py`、`tests/test_tool_lifecycle_p0_2.py` | 四入口共享中央 Tool 生命周期；cooperative cancel 与可恢复/fatal failure 已区分 |
| Trace v2 | `agent/trace_v2.py`、`agent/event_log.py`、`tests/test_trace_v2.py` | Forge 自有最小 tracing schema，跨入口一致并在写盘边界脱敏 |
| Context Policy B1 | `evals/results/context_policy_benchmark/report.json` | 冻结 fixture 上 hybrid 7/7；不是总体胜率 |
| B2 Agent v3 | `evals/results/context_policy_agent_ablation_v3/` | 9 个单次真实 run；`n=3` 且模型非确定 |
| Repo Map retrieval | `evals/results/repo_map_ablation/report.json` | 固定 12-case 集上 MRR/recall 改善 |
| Runner/PR | Trace、verifier、PR 记录 | 一个真实案例完成确定性交付闭环 |
| termination | B2 tests、Trace | 可恢复拒绝、INCOMPLETE、FAILED、GAVE_UP、CANCELED 已区分 |

## 6. 明确延期与非目标

- 完整 Resource Manager：总 token、wall-clock、成本预算与统一调度。
- 强制终止任意同步 Tool/subprocess/provider call 的通用 async runtime 重写。
- hidden-verifier feedback 回灌。
- C6 `context_recall(event_ref)`，除非新 benchmark 证明必要。
- MCP、通用 Skill 平台、多 Agent、多工具并行调用。
- tree-structured session、自动 merge、无人监督发布。
- 为改善 pass@1 静默重跑、修改 fixture 或改变冻结实验变量。

## 7. 工作规则

- 每批开始前以 `TODO-P0-P1.md` 的状态为准，不从历史日志恢复待办。
- 先复现、再修改；只运行与风险相称的测试。
- 真实模型实验必须先冻结模型、fixture、预算、重复数和输出目录。
- 首次失败、修复和定向重跑均如实记录；不覆盖旧实验目录。
- 不修改 `config/default.yaml`；不处理现有 stash；不为测试结果修改 B1/B2 fixture。
