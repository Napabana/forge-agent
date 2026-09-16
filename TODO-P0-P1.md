# Forge Agent P0/P1 当前状态

> 状态基线：2026-09-16。P0-3 本轮实现以 `dev@18fb0cc39b09d428a069b79c3f925875bcdd7ffe` 为起点，
> Trace v2 生产代码已收口；本文件只保留当前结论和剩余工作。历史过程见 [`docs/README.md`](docs/README.md)。

状态定义：`DONE` 已有代码与对应回归覆盖；`PARTIAL` 核心能力存在但仍有明确缺口；
`TODO` 尚未实施；`DEFERRED` 已有意延后，不属于当前主线验收。

> 测试说明：P0-3 的回归测试已补齐，但当前 GitHub connector 无仓库执行环境，pytest 尚未由本轮远程会话实际运行；
> 用户 pull 后执行文末/变更日志中的命令做最终本地验证。若发现回归，P0-3 应立即重新打开而不是隐藏失败。

## 状态总览

| 项目 | 状态 | 当前结论 |
| --- | --- | --- |
| P0-1 `prepare_next_turn` / shared-history 边界 | DONE | 策略插槽、首轮共享历史 preflight 和多轮接线已落地 |
| P0-2 Tool Hook 生产语义 | PARTIAL | 核心顺序与错误分类已有，产品入口一致性和更完整取消语义待补 |
| P0-3 Trace v2 最小闭环 | DONE | v2 schema/correlation、写盘级脱敏、prompt token 分区、termination/acceptance/delivery 已统一；旧 JSONL 只读兼容 |
| P1-1 统一 Runner 与独立验收 | DONE | Runner、acceptance、交付门禁及真实 PR 闭环均有证据 |
| P1-2 Context Compaction | DONE | C1-C5、B1 与 B2 均已完成；C6 单独延期 |
| P1-3 Session 加固 | DONE | 并发占用、恢复与共享历史边界已有实现和测试 |
| P1-4 固定 Harness 任务集 | PARTIAL | 编码/上下文 fixture 和真实 Agent 样本已有，失败注入覆盖仍不完整 |
| P1-5 Repo Map 核心能力 | DONE | query-aware 排序、同 run 写后刷新和正式消融已完成 |
| P1-6 面试证据包 | PARTIAL | 真实 PR、Trace、报告齐备，统一可复现入口与叙事仍可精简 |

## P0-1：`prepare_next_turn` 与共享历史边界 — DONE

- [x] `prepare_next_turn` 是显式策略插槽，不把压缩逻辑硬编码进入口。
- [x] fresh run 与 shared-history run 分流；已有共享历史在第一次模型调用前执行同一 Context Policy preflight。
- [x] Agent 内后续 step 继续复用相同生命周期。
- [x] canonical History 与 model-facing view 分离，压缩不破坏审计历史。
- [x] Chat/Session/Runner 回归覆盖共享历史传递和 round preflight。

证据：`agent/runner.py`、`context/history.py`、`tests/test_chat.py`、`tests/test_compaction.py`。

## P0-2：Tool Hook 生产语义 — PARTIAL

已完成：

- [x] 中央执行链固定为 hook → permission → tool → post-hook。
- [x] permission denial、tool failure 和 hook failure 有稳定错误类型。
- [x] 核心 Harness 有集中回归测试。

剩余：

- [ ] 审计 CLI、Chat、API、GitHub Issue 四入口是否全部暴露一致的 hook 行为。
- [ ] 明确取消发生在 pre-hook、permission、tool 和 post-hook 各阶段时的最终状态与 Trace。
- [ ] 固化入口 × hook × permission × cancel 的参数化离线行为矩阵。

说明：Trace v2 的 schema 级凭据脱敏已在 P0-3 完成；P0-2 不再重复设计日志脱敏，只关注工具生命周期语义。

证据：`harness/executor.py`、`tests/test_harness.py`。

## P0-3：Trace v2 最小闭环 — DONE

已完成：

- [x] 新写入事件统一携带 `trace_schema_version=2`，并保留 `schema_version=2` 兼容别名。
- [x] Run 使用稳定 `run_id` / `run_span_id`；Model、Tool、Context/Compaction 使用 child span，包含 `step_id`、`span_id`、`parent_span_id` 与 operation-specific id。
- [x] CLI、Chat、API、GitHub Issue 通过统一 `ExecutionRunner` 解析/传播 `entrypoint`；isolate 路径使用轻量 `ContextVar` 传播，不侵入 orchestrator 生命周期。
- [x] EventLog 最终 JSONL 写盘边界统一递归 redaction；覆盖 Authorization/API Key/GitHub token/password/secret/token 字段，以及 Bearer、`sk-`、`ghp_`、`github_pat_` 常见字符串模式。
- [x] token usage 字段白名单保护，`input_tokens` / `output_tokens` / `cached_tokens` / `total_tokens` 等统计不会被误删。
- [x] Model span 记录本地诊断 `token_breakdown`：system、tool schema、repo map、history/context、pending、injected、estimated input；Provider 实际 usage 单独记录为 `provider_usage`，不与 estimate 混用。
- [x] provider error、infrastructure error、cancel、loop detected、resource exhausted、completion rejection、acceptance、delivery 均可落到统一 run correlation 下。
- [x] Runner 统一追加 `acceptance` 与 `run_terminated`；GitHub Issue 在真实 commit/push/PR 决策后追加 `delivery`。
- [x] 旧 JSONL 读取/追加保持只读兼容：历史行不迁移、不重写；新追加行才使用 v2 schema。
- [x] B1/B2 reader 继续按事件类型和可选字段消费；历史 `evals/results` 未改写。
- [x] 回归覆盖集中扩展在 `tests/test_trace_v2.py` 与既有 GitHub delivery 测试，包含 schema、correlation、parent/child、model/tool/compaction、termination、acceptance/delivery、四入口、redaction、token breakdown、provider error、completion rejection、INCOMPLETE、legacy JSONL。

已知边界（不影响 P0-3 完成）：

- Trace v2 是 Forge 自有最小 schema，不引入 OpenTelemetry SDK、collector、外部数据库或云 tracing 服务。
- 本地 token breakdown 是诊断估算，不代表 provider 计费真值；真实 usage 以 provider 返回为准。
- 不全量保存/复制 provider request payload，也不在本轮实现 MCP、多 Agent、multi-tool call 或 Resource Manager。
- 本轮远程连接无法执行 pytest；本地测试若发现失败则重新打开 P0-3。

证据：`agent/trace_v2.py`、`agent/event_log.py`、`agent/core.py`、`agent/runner.py`、`entry/github_issue.py`、
`tests/test_trace_v2.py`、`tests/test_github_issue_delivery.py`、`docs/changes/2026-09-16/Trace-v2收口改动内容.md`。

## P1-1：统一 Runner、独立验收与真实 PR — DONE

- [x] `ExecutionRunner` 统一运行请求、Trace、结果和 acceptance contract。
- [x] hidden verifier 不进入 canonical History，只有独立验收通过才允许交付。
- [x] `--no-pr` 与自动 PR registry 的提交权边界有测试。
- [x] clone/push 使用临时认证 header，remote 和日志不保存 Token。
- [x] 真实案例完成 Agent → verifier → commit → push → PR → merge 闭环。

证据：`agent/runner.py`、`entry/github_issue.py`、`tests/test_runner.py`、`tests/test_github_issue_delivery.py`、
`evals/pr_test_issue_4_verifier.py`，以及
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
- [x] 正式 retrieval 消融：static MRR 0.097、query-aware MRR 0.319；预算内目标召回 0.365 → 0.635。
- [x] 引用计数性能消融：优化前 median 35.118s，优化后 0.493s；语义等价，约 71.3×。

证据：`context/repo_map.py`、`tests/test_repo_map_improvements.py`、
[`evals/results/repo_map_ablation/report.json`](evals/results/repo_map_ablation/report.json)。

已知边界：

- `PARTIAL`：Repo Map cache identity 尚未显式包含 Git HEAD/working-tree fingerprint。
- `PARTIAL`：框架文件工具写后刷新已覆盖；shell/git 引起的删除、重命名或写入未统一感知。
- `PARTIAL`：解析失败存在 best-effort fallback，但“陈旧 map 不误导 Agent”的契约仍可强化。
- `DEFERRED`：directory-tree-only baseline、first-effective-edit token 指标和更大规模真实 Agent 对照属于可选后续证据，不是本阶段完成门槛。

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

1. P0-2：工具 Hook / permission / cancel 四入口一致性与阶段语义。
2. P1-4：用离线失败注入补全 Harness 可靠性任务集。
3. P1-6：整理可重复、不过度宣称的面试证据入口。
4. P1-5 小尾项：仅在测试能稳定复现时再处理 shell/git 写入、删除/重命名与 cache identity。
