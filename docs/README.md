# Forge Agent 文档索引

本目录把“当前事实”和“历史过程”分开。开始新任务时优先阅读 source of truth；只有需要追溯
设计决策、测试过程或实验原始证据时才进入历史日志。

## Source of truth

- [`../AGENTS.md`](../AGENTS.md)：本机协作约束、环境、当前状态和最后交接。
- [`../TODO-P0-P1.md`](../TODO-P0-P1.md)：P0/P1 当前状态、真实剩余项和延期项。
- [`../Forge-Agent-P0-P1-实施计划.md`](../Forge-Agent-P0-P1-实施计划.md)：下一批实施顺序与验收原则。
- [`../README.md`](../README.md)：项目能力、安装与使用入口。
- [`../USAGE.md`](../USAGE.md)：详细运行配置和命令。

## 设计文档

- [`design/Forge-Agent-当前代码架构.md`](design/Forge-Agent-当前代码架构.md)：当前源码分层、调用链和边界。

## 历史计划

以下文件只保留原始决策与施工过程，不代表当前待办：

- [`plans/archive/CONTEXT_COMPACTION_EXECUTION_PLAN.md`](plans/archive/CONTEXT_COMPACTION_EXECUTION_PLAN.md)
- [`plans/archive/CONTEXT_COMPACTION_TODO.md`](plans/archive/CONTEXT_COMPACTION_TODO.md)
- [`plans/archive/TODO-P0-P1-2026-09-16-before-alignment.md`](plans/archive/TODO-P0-P1-2026-09-16-before-alignment.md)
- [`plans/archive/Forge-Agent-P0-P1-实施计划-2026-09-16-before-alignment.md`](plans/archive/Forge-Agent-P0-P1-实施计划-2026-09-16-before-alignment.md)

## 变更记录

### 2026-09-14

- [`changes/2026-09-14/P0-1-P0-2与Trace-v2最小闭环.md`](changes/2026-09-14/P0-1-P0-2与Trace-v2最小闭环.md)
- [`changes/2026-09-14/P0集中边界测试与错误分类收口.md`](changes/2026-09-14/P0集中边界测试与错误分类收口.md)
- [`changes/2026-09-14/Repo-Map引用计数热点修复与实施计划.md`](changes/2026-09-14/Repo-Map引用计数热点修复与实施计划.md)
- [`changes/2026-09-14/Runner与可追溯Compaction实施.md`](changes/2026-09-14/Runner与可追溯Compaction实施.md)
- [`changes/2026-09-14/Token-Usage-统计改动.md`](changes/2026-09-14/Token-Usage-统计改动.md)

### 2026-09-15

- Context Compaction：[`C1`](changes/2026-09-15/Context-Compaction-C1改动内容.md)、
  [`C2`](changes/2026-09-15/Context-Compaction-C2改动内容.md)、
  [`C3`](changes/2026-09-15/Context-Compaction-C3改动内容.md)、
  [`C3 设计`](changes/2026-09-15/Context-Compaction-C3设计收口.md)、
  [`C4`](changes/2026-09-15/Context-Compaction-C4改动内容.md)、
  [`C4 设计`](changes/2026-09-15/Context-Compaction-C4设计收口.md)、
  [`C5`](changes/2026-09-15/Context-Compaction-C5改动内容.md)、
  [`C5 设计`](changes/2026-09-15/Context-Compaction-C5设计收口.md)、
  [`规划收口`](changes/2026-09-15/Context-Compaction规划收口.md)、
  [`B1 设计`](changes/2026-09-15/Context-Policy-B1设计收口.md)。
- Runner/Session：[`Runner 独立验收`](changes/2026-09-15/Runner独立验收闭环.md)、
  [`Session 加固与 Query-aware 评测`](changes/2026-09-15/Session加固与Query-aware评测收口.md)、
  [`Session 双进程冲突`](changes/2026-09-15/Session双进程冲突收口.md)。
- Repo Map：[`Query Ranking`](changes/2026-09-15/RepoMap-QueryRanking改动内容.md)、
  [`同一 Run 写后刷新`](changes/2026-09-15/RepoMap同一Run写后刷新.md)。
- 自动 PR：[`真实 PR`](changes/2026-09-15/pr-test真实PR改动内容.md)、
  [`确定性交付`](changes/2026-09-15/自动PR确定性交付闭环.md)、
  [`Token-safe clone`](changes/2026-09-15/Token安全Clone改动内容.md)、
  [`--no-pr 契约`](changes/2026-09-15/no-pr兼容契约改动内容.md)、
  [`合并后规划`](changes/2026-09-15/真实PR合并后续规划改动内容.md)。
- 其他：[`EvalRunner 证据`](changes/2026-09-15/EvalRunner证据闭环.md)、
  [`失败样本统计`](changes/2026-09-15/失败样本统计改动内容.md)、
  [`FGH/P0-1 规划`](changes/2026-09-15/FGH与P0-1剩余工作规划.md)、
  [`P0-1 准备`](changes/2026-09-15/P0-1下一轮准备收口.md)、
  [`架构文档变更`](changes/2026-09-15/当前代码架构文档改动内容.md)、
  [`项目 Plugin 精简`](changes/2026-09-15/项目级Plugin精简改动内容.md)。

### 2026-09-16

- [`changes/2026-09-16/B2评测与Agent终止策略收口.md`](changes/2026-09-16/B2评测与Agent终止策略收口.md)：B2 设计与验收协议。
- [`changes/2026-09-16/B2一次性执行交接-Codex.md`](changes/2026-09-16/B2一次性执行交接-Codex.md)：一次性执行交接。
- [`changes/2026-09-16/B2终止语义与v3评测改动内容.md`](changes/2026-09-16/B2终止语义与v3评测改动内容.md)：B2 实现、测试和 v3 结果。
- [`changes/2026-09-16/文档与TODO状态整理改动内容.md`](changes/2026-09-16/文档与TODO状态整理改动内容.md)：状态对齐和文档归档。
- [`changes/2026-09-16/Trace-v2收口改动内容.md`](changes/2026-09-16/Trace-v2收口改动内容.md)：P0-3 Trace v2 schema、correlation、redaction、token breakdown、兼容与本地验证命令。
- [`changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`](changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md)：P0-2 Tool lifecycle、错误分类、cooperative cancel、四入口/direct-isolate 一致性与 Trace v2 对齐。

## 评测证据

评测原始输出保持在 `evals/results/`，不移动到文档目录：

- [`../evals/results/context_policy_benchmark/report.json`](../evals/results/context_policy_benchmark/report.json)：B1，7 cases × 3 variants 的离线 fixture benchmark。
- [`../evals/results/context_policy_agent_ablation_v2/report.json`](../evals/results/context_policy_agent_ablation_v2/report.json)：B2 v2 对照基线。
- [`../evals/results/context_policy_agent_ablation_v3/report.md`](../evals/results/context_policy_agent_ablation_v3/report.md)：B2 v3 人类可读报告；同目录保留 `raw.jsonl`、`report.json` 和 traces。
- [`../evals/results/repo_map_ablation/report.json`](../evals/results/repo_map_ablation/report.json)：Repo Map retrieval 与性能消融。

## 阅读规则

- 判断“现在做什么”只看根目录 TODO 和实施计划。
- 历史日志中的“下一步”保留当时语境，不自动恢复为当前待办。
- fixture、单次真实案例和小样本 Agent 实验必须按各自证据边界表述，不外推总体成功率。
