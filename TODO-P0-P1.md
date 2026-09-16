# Forge Agent P0/P1 当前状态

> 状态基线：2026-09-16，`dev` @ `ba0720d3ca909afd0146510bd5bad7cf8131d2d6`。
> 本文件只保留当前结论和剩余工作；历史过程见 [`docs/README.md`](docs/README.md)。

状态定义：`DONE` 已有代码、测试或正式实验依据；`PARTIAL` 核心能力存在但仍有明确缺口；
`TODO` 尚未实施；`DEFERRED` 已有意延后，不属于当前主线验收。

## 状态总览

| 项目 | 状态 | 当前结论 |
| --- | --- | --- |
| P0-1 `prepare_next_turn` / shared-history 边界 | DONE | 策略插槽、首轮共享历史 preflight 和多轮接线已落地 |
| P0-2 Tool Hook 生产语义 | PARTIAL | 核心顺序与错误分类已有，产品入口一致性和更完整取消语义待补 |
| P0-3 Trace v2 最小闭环 | PARTIAL | 核心事件、usage、termination 与 acceptance 已有，schema/脱敏/统计仍未完全统一 |
| P1-1 统一 Runner 与独立验收 | DONE | Runner、acceptance、交付门禁及真实 PR 闭环均有证据 |
| P1-2 Context Compaction | DONE | C1-C5、B1 与 B2 均已完成；C6 单独延期 |
| P1-3 Session 加固 | DONE | 并发占用、恢复与共享历史边界已有实现和测试 |
| P1-4 固定 Harness 任务集 | PARTIAL | 编码/上下文 fixture 和真实 Agent 样本已有，失败注入覆盖仍不完整 |
| P1-5 Repo Map 核心能力 | DONE | query-aware 排序、同 run 写后刷新和正式消融已完成 |
| P1-6 面试证据包 | PARTIAL | 真实 PR、Trace、报告齐备，统一可复现入口与叙事仍可精简 |

## P0-1：`prepare_next_turn` 与共享历史边界 — DONE

- [x] `prepare_next_turn` 是显式策略插槽，不把压缩逻辑硬编码进入口。
- [x] fresh run 与 shared-history run 分流；已有共享历史在第一次模型调用前执行同一
  Context Policy preflight。
- [x] Agent 内后续 step 继续复用相同生命周期。
- [x] canonical History 与 model-facing view 分离，压缩不破坏审计历史。
- [x] Chat/Session/Runner 回归覆盖共享历史传递和 round preflight。

证据：`agent/runner.py`、`context/history.py`、`tests/test_chat.py`、
`tests/test_compaction.py`，以及提交 `0376112`。

## P0-2：Tool Hook 生产语义 — PARTIAL

已完成：

- [x] 中央执行链固定为 hook → permission → tool → post-hook。
- [x] permission denial、tool failure 和 hook failure 有稳定错误类型。
- [x] 核心 Harness 有集中回归测试。

剩余：

- [ ] 审计 CLI、Chat、API、GitHub Issue 四入口是否全部暴露一致的 hook 行为。
- [ ] 明确取消发生在 pre-hook、tool 和 post-hook 各阶段时的最终状态与 Trace。
- [ ] 为敏感参数建立统一的输入/输出脱敏契约，而非依赖各工具自行处理。

证据：`harness/executor.py`、`tests/test_harness.py`。

## P0-3：Trace v2 最小闭环 — PARTIAL

已完成：

- [x] model、tool、compaction、usage、completion rejection、termination、acceptance 和
  delivery 已有可审计事件或结构化字段。
- [x] B2 报告可汇总 status、termination/resource reason、rejection、token 与 latency。
- [x] canonical History、ephemeral resource warning 与 hidden verifier 的边界已固定。

剩余：

- [ ] 固化跨入口统一的 Trace schema/version，而不是由报告层兼容字段差异。
- [ ] 补齐 prompt 分区 token 统计和 schema 级脱敏测试。
- [ ] 固化取消、provider 空响应和基础设施失败的跨入口统计口径。

## P1-1：统一 Runner、独立验收与真实 PR — DONE

- [x] `ExecutionRunner` 统一运行请求、Trace、结果和 acceptance contract。
- [x] hidden verifier 不进入 canonical History，只有独立验收通过才允许交付。
- [x] `--no-pr` 与自动 PR registry 的提交权边界有测试。
- [x] clone/push 使用临时认证 header，remote 和日志不保存 Token。
- [x] 真实案例完成 Agent → verifier → commit → push → PR → merge 闭环。

证据：`agent/runner.py`、`entry/github_issue.py`、`tests/test_runner.py`、
`tests/test_github_issue_delivery.py`、`evals/pr_test_issue_4_verifier.py`，以及
[`docs/changes/2026-09-15/pr-test真实PR改动内容.md`](docs/changes/2026-09-15/pr-test真实PR改动内容.md)。

## P1-2：Context Compaction — DONE（C6 延期）

### C1-C5 — DONE

- [x] C1：canonical History 与 model-facing view 分离。
- [x] C2：`HistoryUnit`、token budget 和稳定消息单元。
- [x] C3：请求前预算、repository fingerprint、可追踪压缩 lineage。
- [x] C4：tool observation pruning、引用保护与孤儿事件检查。
- [x] C5：结构化/语义压缩、usage 汇总、Trace 和 Runner 集成。

历史计划已归档到 [`docs/plans/archive/`](docs/plans/archive/)，不再作为当前待办清单。

### B1 离线 Context Policy benchmark — DONE

- 7 cases × 3 variants = 21 个 fixture 样本。
- budget trim：5/7；deterministic pruning：6/7；hybrid：7/7。
- Hybrid 的 hard-constraint recall、recent-state recall 均为 1.0，orphan action/observation 为 0。
- 这是固定 fixture 离线结果，不代表任意真实模型任务的总体胜率。

证据：[`evals/results/context_policy_benchmark/report.json`](evals/results/context_policy_benchmark/report.json)。

### B2 Agent termination 与 v3 — DONE

- [x] Completion Guard 可恢复拒绝会写结构化 Trace 与 canonical History，并在有资源时继续。
- [x] 状态区分 `SUCCESS`、`INCOMPLETE`、`FAILED`、`GAVE_UP`、`CANCELED`。
- [x] max steps 使用 `resource_exhausted/max_steps`；Loop Detector 不冒充显式 `GAVE_UP`。
- [x] `[RESOURCE BUDGET LOW]` 不暴露精确倒计时且不进入 canonical History。
- [x] v3 按冻结参数完成 3 cases × 3 variants × 1 run，共 9 个真实 run。

v3：baseline strict 1/3、verifier 2/3；pruning 2/3、2/3；hybrid 2/3、3/3。
该结果仅为 `n=3`、单次运行，受模型非确定性影响，不能据此断言 Hybrid 普遍最优。

证据：[`evals/results/context_policy_agent_ablation_v3/report.md`](evals/results/context_policy_agent_ablation_v3/report.md)
和 [`docs/changes/2026-09-16/B2终止语义与v3评测改动内容.md`](docs/changes/2026-09-16/B2终止语义与v3评测改动内容.md)。

### C6 `context_recall(event_ref)` — DEFERRED

仅当稳定 benchmark 证明现有 model-facing view 无法可靠保留必要证据时再启动；当前不扩展。

## P1-3：Session 加固 — DONE

- [x] Session 状态持久化、恢复和共享历史增长有回归测试。
- [x] 双进程/并发占用边界已收口。
- [x] shared-history 首轮 preflight 与 Context Policy 接线完成。

证据：`entry/chat.py`、`tests/test_chat.py`、`tests/test_compaction.py` 和
[`docs/changes/2026-09-15/Session双进程冲突收口.md`](docs/changes/2026-09-15/Session双进程冲突收口.md)。

## P1-4：固定 Harness 任务集 — PARTIAL

已完成：

- [x] 编码、长约束、巨大工具历史、过期状态等固定 fixture。
- [x] Context Policy B1 离线 benchmark 与 B2 真实 Agent ablation。
- [x] 真实 PR 的仓库外 verifier 和失败样本记录。

剩余：

- [ ] 建立 provider timeout/空响应、permission denial、hook failure、取消等统一失败注入矩阵。
- [ ] 固化一条不调用付费模型的日常回归入口，并明确真实模型实验只在冻结协议后执行。

## P1-5：Repo Map 一致性、相关性与消融 — DONE（边界另列）

核心已完成：

- [x] Query relevance 同时利用 path、symbol 与已扫描 source text。
- [x] Agent 以 task description 驱动 query-aware Repo Map。
- [x] `file_write` / `file_edit` / `edit` 成功后失效缓存，同一 run 可见新内容。
- [x] 内容命中、路径/符号命中、空 query、选择性失效和写后刷新均有测试。
- [x] 正式 retrieval 消融：static MRR 0.097、query-aware MRR 0.319；预算内目标召回
  0.365 → 0.635。
- [x] 引用计数性能消融：优化前 median 35.118s，优化后 0.493s；语义等价，约 71.3×。

证据：`context/repo_map.py`、`tests/test_repo_map_improvements.py`、
[`evals/results/repo_map_ablation/report.json`](evals/results/repo_map_ablation/report.json)。

已知边界：

- `PARTIAL`：Repo Map cache identity 尚未显式包含 Git HEAD/working-tree fingerprint。
- `PARTIAL`：框架文件工具写后刷新已覆盖；shell/git 引起的删除、重命名或写入未统一感知。
- `PARTIAL`：解析失败存在 best-effort fallback，但“陈旧 map 不误导 Agent”的契约仍可强化。
- `DEFERRED`：directory-tree-only baseline、first-effective-edit token 指标和更大规模真实 Agent
  对照属于可选后续证据，不是本阶段完成门槛。

## P1-6：自动 PR 与面试证据包 — PARTIAL

已完成真实 PR、独立 verifier、Trace、失败样本与 benchmark 报告。剩余工作只做证据产品化：

- [ ] 提供一个可重复执行、默认离线的证据索引/命令入口。
- [ ] 将“实现事实”“单样本案例”“正式消融”“仍未知”拆成稳定面试表述。
- [ ] 不把一次真实 PR、`n=3` Agent ablation 或 fixture benchmark 外推为总体成功率。

## 明确延期或不在当前计划

- `DEFERRED`：完整 Resource Manager（总 token、wall-clock、成本硬预算）。
- `DEFERRED`：hidden-verifier feedback 回灌 Agent。
- `DEFERRED`：MCP、通用 Skill 市场、多 Agent、多工具并行调用。
- `DEFERRED`：tree-structured session、自动 merge、无人监督发布。
- `DEFERRED`：为提升指标而静默重跑、调整 benchmark 或扩大付费调用范围。

## 下一批建议顺序

1. P0-3：统一 Trace schema/version 与跨入口脱敏契约。
2. P0-2：补齐四入口 hook/cancel 一致性矩阵。
3. P1-4：用离线失败注入补全 Harness 可靠性任务集。
4. P1-5 小尾项：仅在测试能稳定复现时补 shell/git 写入与删除/重命名失效。
5. P1-6：整理可重复、不过度宣称的面试证据入口。
