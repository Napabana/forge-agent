# Forge Agent P0/P1 实施计划（当前版）

> 更新基线：2026-09-16，`dev` @ `ba0720d3ca909afd0146510bd5bad7cf8131d2d6`。
> 当前状态的唯一清单是 [`TODO-P0-P1.md`](TODO-P0-P1.md)；本文说明已完成主线、下一批
> 实施顺序和明确延期项，不再保留已经失效的逐步施工指令。

## 1. 当前结论

Forge Agent 已完成这一阶段最关键的生命周期闭环：统一 Runner、独立 acceptance、可追溯
Context Compaction、query-aware Repo Map、Session 加固、真实 PR 交付，以及 B2 termination
语义和真实 Agent v3 实验。

下一批不应继续扩张 Agent 能力面，而应优先统一 Trace/Hook 的跨入口契约，并补齐可离线、
可重复的失败注入证据。完整 Resource Manager、hidden-verifier feedback、MCP 和多 Agent 均不在
本计划当前执行范围。

## 2. 已完成主线

### 2.1 生命周期与 Runner

- `prepare_next_turn` 已作为策略插槽接入 Agent step 和 shared-history run 首轮 preflight。
- `ExecutionRunner` 已统一 run、Trace、acceptance 和结果边界。
- hidden verifier 与 canonical History 隔离；验收不通过不得进入 delivery。
- 自动 PR 已完成真实仓库的 Agent → verifier → commit → push → PR → merge 案例。

结论：P0-1、P1-1 已完成；不再重复设计新的 Runner 抽象。

### 2.2 Context Compaction

- C1-C5 已完成：canonical/model view 分离、HistoryUnit、预算与 fingerprint、tool pruning、
  structured/semantic compaction、usage/Trace/Runner 集成。
- B1 离线 benchmark 已完成 7×3=21 个 fixture 样本；hybrid 为 7/7，但只代表该冻结任务集。
- B2 termination 已完成，v3 已按冻结参数一次执行 3×3×1=9 个真实 run。
- C6 `context_recall(event_ref)` 延期，只有稳定 benchmark 出现明确需求时才重启。

结论：P1-2 已完成。历史施工计划保存在 [`docs/plans/archive/`](docs/plans/archive/)，不再作为
当前 backlog。

### 2.3 Repo Map

- query-aware ranking 使用 path、symbol 和 source text。
- 文件工具成功写入后失效缓存，同一 run 可重新构建。
- 正式 retrieval 消融：MRR 0.097 → 0.319，预算内目标召回 0.365 → 0.635。
- 引用计数优化的正式性能消融：median 35.118s → 0.493s，语义等价，约 71.3×。

结论：P1-5 核心完成。cache identity、shell/git 写入感知和更大规模 Agent 对照属于边界或可选
证据，不能倒推核心仍未完成。

### 2.4 Session、Harness 与真实交付

- Session 恢复、共享历史和双进程占用边界已加固。
- 固定 context-policy fixture、真实 Agent ablation、仓库外 verifier 和真实 PR 证据已存在。
- Token-safe clone/push 与 `--no-pr` 提交权边界已有测试。

结论：P1-3 完成；P1-4、P1-6 因失败注入矩阵和证据产品化尚未完成而保持 `PARTIAL`。

## 3. 下一批执行顺序

### Batch A：Trace v2 跨入口收口（P0-3）

目标：让 CLI、Chat、API、GitHub Issue 和 eval 报告消费同一最小 schema。

1. 盘点现有 event type 与字段，冻结 schema version 和必填/可选字段。
2. 统一 model/tool/compaction/termination/acceptance/delivery 的 correlation 字段。
3. 建立 schema 级 secret redaction 测试和 prompt 分区 token 统计。
4. 对旧 JSONL 保持只读兼容；不重写历史结果。

验收：离线 schema 测试覆盖四入口；旧 B1/B2 报告仍可读取；日志不出现凭据。

### Batch B：Tool Hook / cancel 一致性（P0-2）

目标：把已有中央执行顺序变成四入口一致的生产契约。

1. 建立入口 × pre-hook × permission × tool × post-hook × cancel 的行为矩阵。
2. 仅修复可复现的不一致，不重新设计 hook framework。
3. 固化每类失败的 RunStatus、termination reason 和 Trace 事件。

验收：不调用真实 provider 的参数化回归通过；既有工具错误分类不回退。

### Batch C：固定 Harness 失败任务集（P1-4）

目标：把 provider/infrastructure/permission/hook/cancel 边界变成默认离线回归。

1. 增加 deterministic fake provider 与 failure injection fixture。
2. 为每类失败固定期望 status、termination reason、acceptance/delivery 行为。
3. 复用现有 EvalRunner，避免再造第二套运行器。

验收：单命令离线执行；结果可复现；真实模型实验与日常测试明确分开。

### Batch D：Repo Map 小尾项（条件执行）

仅在最小测试能稳定复现以下问题时实施：

- cache identity 未包含 Git HEAD/working-tree fingerprint 导致错误复用；
- shell/git 写入、删除或重命名导致同 run map 陈旧；
- parser fallback 导致 Agent 把陈旧 map 当成强事实。

不为追求更漂亮指标追加付费实验。directory-tree-only baseline、first-effective-edit token 和更大
真实 Agent 对照作为可选证据单独提案。

### Batch E：证据包产品化（P1-6）

1. 用 `docs/README.md` 维持 source-of-truth 与证据导航。
2. 提供默认离线、显式 opt-in 真实模型的复现实验入口说明。
3. 将实现、测试、fixture benchmark、真实案例和限制分别陈述。

## 4. 证据基线

| 能力 | 证据 | 可安全表述 |
| --- | --- | --- |
| Context Policy B1 | `evals/results/context_policy_benchmark/report.json` | 冻结 fixture 上 hybrid 7/7；不是总体胜率 |
| B2 Agent v3 | `evals/results/context_policy_agent_ablation_v3/` | 9 个单次真实 run；`n=3` 且模型非确定 |
| Repo Map retrieval | `evals/results/repo_map_ablation/report.json` | 固定 12-case 集上 MRR/recall 改善 |
| Repo Map 性能 | 同一 report 的 performance 部分 | 固定 commit 对照中约 71.3×，语义等价 |
| Runner/PR | Trace、verifier、PR 记录 | 一个真实案例完成确定性交付闭环 |
| termination | B2 tests、raw/report/Trace | 可恢复拒绝、INCOMPLETE 与 fatal failure 已区分 |

## 5. 明确延期与非目标

- 完整 Resource Manager：总 token、wall-clock、成本预算与统一调度。
- hidden-verifier feedback 回灌。
- C6 `context_recall(event_ref)`，除非新 benchmark 证明必要。
- MCP、通用 Skill 平台、多 Agent、多工具并行调用。
- tree-structured session、自动 merge、无人监督发布。
- 为改善 pass@1 静默重跑、修改 fixture 或改变冻结实验变量。

## 6. 工作规则

- 每批开始前以 `TODO-P0-P1.md` 的状态为准，不从历史日志恢复待办。
- 先复现、再修改；只运行与风险相称的测试。
- 真实模型实验必须先冻结模型、fixture、预算、重复数和输出目录。
- 首次失败、修复和定向重跑均如实记录；不覆盖旧实验目录。
- 不自动 pull/rebase/push/commit，不修改 `config/default.yaml`，不处理现有 stash。
