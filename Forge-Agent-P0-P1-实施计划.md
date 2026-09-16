# Forge Agent P0/P1 实施计划（当前版）

> 更新基线：2026-09-16。当前状态的唯一清单是 [`TODO-P0-P1.md`](TODO-P0-P1.md)；
> 本文只说明已完成主线、下一批实施顺序和明确延期项。

## 1. 当前结论

Forge Agent 当前已经完成这一阶段最关键的生命周期与可观测闭环：统一 Runner、独立 acceptance、
Trace v2、可追溯 Context Compaction、query-aware Repo Map、Session 加固、真实 PR 交付，以及
B2 termination 语义和真实 Agent v3 实验。

P0-3 Trace v2 已完成实现收口：四入口通过同一 Runner/EventLog schema，run/model/tool/context/
completion/acceptance/delivery 有统一 correlation；敏感字段在 JSONL 写盘边界统一脱敏；模型调用可
拆分本地 prompt token 估算，同时保留 provider 实际 usage；旧 JSONL 与历史 B1/B2 结果保持只读兼容。

当前不再扩张 Trace 功能面。下一批切换到 P0-2，只收口已有 Tool Hook / Permission / Cancel 的生产语义。
完整 Resource Manager、hidden-verifier feedback、MCP、多 Agent、多工具并行仍不在当前范围。

> 验证状态：P0-3 回归测试代码已补齐，但当前 GitHub connector 无可执行仓库环境；用户 pull 后在本地
> 运行定向测试与全量 pytest。若出现回归，重新打开 P0-3，不通过修改 fixture 或历史结果规避失败。

## 2. 已完成主线

### 2.1 生命周期、Runner 与 Trace v2

- `prepare_next_turn` 已作为策略插槽接入 Agent step 和 shared-history run 首轮 preflight。
- `ExecutionRunner` 统一 run、Trace、acceptance 和结果边界。
- hidden verifier 与 canonical History 隔离；验收不通过不得进入 delivery。
- 自动 PR 已完成真实仓库的 Agent → verifier → commit → push → PR → merge 案例。
- Trace v2 统一 `trace_schema_version=2`、`run_id/run_span_id`、child span、entrypoint、termination、
  acceptance 与 GitHub delivery。
- EventLog 写盘边界负责 schema 级 redaction，不依赖单个 Tool；usage token 字段有显式保护。
- Model span 分离本地 `token_breakdown` 与 `provider_usage`，避免估算值冒充真实计费 usage。
- 旧 JSONL 不迁移、不重写；历史 reader 仍可按原事件字段消费。

结论：P0-1、P0-3、P1-1 已完成；不再重复设计新的 Runner 或 tracing framework。

### 2.2 Context Compaction

- C1-C5 已完成：canonical/model view 分离、HistoryUnit、预算与 fingerprint、tool pruning、
  structured/semantic compaction、usage/Trace/Runner 集成。
- B1 离线 benchmark 已完成 7×3=21 个 fixture 样本；hybrid 为 7/7，但只代表该冻结任务集。
- B2 termination 已完成，v3 已按冻结参数一次执行 3×3×1=9 个真实 run。
- C6 `context_recall(event_ref)` 延期，只有稳定 benchmark 出现明确需求时才重启。

结论：P1-2 已完成。历史施工计划保存在 [`docs/plans/archive/`](docs/plans/archive/)，不再作为当前 backlog。

### 2.3 Repo Map

- query-aware ranking 使用 path、symbol 和 source text。
- 文件工具成功写入后失效缓存，同一 run 可重新构建。
- 正式 retrieval 消融：MRR 0.097 → 0.319，预算内目标召回 0.365 → 0.635。
- 引用计数优化的正式性能消融：median 35.118s → 0.493s，语义等价，约 71.3×。

结论：P1-5 核心完成。cache identity、shell/git 写入感知和更大规模 Agent 对照属于边界或可选证据。

### 2.4 Session、Harness 与真实交付

- Session 恢复、共享历史和双进程占用边界已加固。
- 固定 context-policy fixture、真实 Agent ablation、仓库外 verifier 和真实 PR 证据已存在。
- Token-safe clone/push 与 `--no-pr` 提交权边界已有测试。

结论：P1-3 完成；P1-4、P1-6 因失败注入矩阵和证据产品化尚未完成而保持 `PARTIAL`。

## 3. 下一批执行顺序

### Batch A：Tool Hook / Permission / Cancel 一致性（P0-2）

目标：把已有中央执行顺序变成 CLI、Chat、API、GitHub Issue 一致的生产契约，不重构 Agent 主循环。

先回答四个问题：

1. `pre-hook → permission → tool → post-hook` 的实际执行顺序在所有入口是否一致；
2. permission deny / confirm、pre-hook block/failure、tool failure、post-hook failure 各自应返回什么 `ToolResult` / `RunStatus`；
3. cancel 在进入 pre-hook 前、permission 后、tool 前后、post-hook 前后各自当前是什么语义；
4. 上述结果如何映射到 Trace v2 的 `tool_execution_failed` 与最终 `termination_reason`，避免同类失败在不同入口产生不同统计。

实施原则：

- 优先扩展 `tests/test_harness.py`、Runner/API/Chat 既有测试，建立参数化行为矩阵；
- 只修复可复现的不一致，不重新设计 Hook framework；
- Trace v2 的 redaction 已完成，P0-2 不再另造敏感信息处理层；
- 不扩大到 Resource Manager、multi-tool call 或多 Agent。

验收：离线 fake tool/fake hook 测试覆盖四入口核心路径；取消与失败状态可解释；不调用真实 provider。

### Batch B：固定 Harness 失败任务集（P1-4）

目标：把 provider/infrastructure/permission/hook/cancel 边界变成默认离线回归。

1. 增加 deterministic fake provider 与 failure injection fixture。
2. 为每类失败固定期望 status、termination reason、acceptance/delivery 行为。
3. 复用现有 EvalRunner/ExecutionRunner，避免第二套生命周期。
4. 提供不调用付费模型的日常回归命令；真实模型实验继续显式 opt-in。

### Batch C：证据包产品化（P1-6）

目标：冻结项目代码前，把已经存在的实现、测试和实验整理成可复现、不过度宣称的面试证据。

1. 用 `docs/README.md` 维护 evidence index。
2. 给出默认离线的复现命令，真实模型 benchmark 单独说明成本与非确定性。
3. 将“实现事实”“单样本真实案例”“固定 fixture benchmark”“小样本真实模型实验”“仍未知”分开。
4. 最终简历只引用能指向代码、测试或正式结果的主张。

### Batch D：Repo Map 小尾项（条件执行）

仅在最小测试能稳定复现以下问题时实施：

- cache identity 未包含 Git HEAD/working-tree fingerprint 导致错误复用；
- shell/git 写入、删除或重命名导致同 run map 陈旧；
- parser fallback 导致 Agent 把陈旧 map 当成强事实。

不为追求更漂亮指标追加付费实验。

## 4. P0-3 本地验证命令

用户 pull 后优先运行：

```bash
pytest tests/test_trace_v2.py tests/test_runner.py tests/test_compaction.py \
  tests/test_agent_completion_guards.py tests/test_chat.py tests/test_api.py \
  tests/test_github_issue_delivery.py -q

pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q

pytest -q
```

其中前两组负责 P0-3 定向回归与 B1/B2 reader 回归；最后一条作为全量确认。

## 5. 证据基线

| 能力 | 证据 | 可安全表述 |
| --- | --- | --- |
| Trace v2 | `agent/trace_v2.py`、`agent/event_log.py`、`tests/test_trace_v2.py` | Forge 自有最小 tracing schema，跨入口一致并在写盘边界脱敏 |
| Context Policy B1 | `evals/results/context_policy_benchmark/report.json` | 冻结 fixture 上 hybrid 7/7；不是总体胜率 |
| B2 Agent v3 | `evals/results/context_policy_agent_ablation_v3/` | 9 个单次真实 run；`n=3` 且模型非确定 |
| Repo Map retrieval | `evals/results/repo_map_ablation/report.json` | 固定 12-case 集上 MRR/recall 改善 |
| Repo Map 性能 | 同一 report 的 performance 部分 | 固定 commit 对照中约 71.3×，语义等价 |
| Runner/PR | Trace、verifier、PR 记录 | 一个真实案例完成确定性交付闭环 |
| termination | B2 tests、raw/report/Trace | 可恢复拒绝、INCOMPLETE 与 fatal failure 已区分 |

## 6. 明确延期与非目标

- 完整 Resource Manager：总 token、wall-clock、成本预算与统一调度。
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
