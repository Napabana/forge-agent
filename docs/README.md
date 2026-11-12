# Forge Agent 文档索引

这是 **Forge Agent — Architecture & Implementation** 的本地知识库首页。网站以当前源码为实现事实，
以 `docs/design/` 中的 Markdown 与 Mermaid 为唯一技术正文，不替代根目录的项目介绍与使用说明。
全文搜索覆盖 Context Compaction、Git Worktree 等跨专题概念，结果始终回到当前知识库页面。

## 整体架构

--8<-- "docs/includes/system-architecture.md.inc"

[阅读完整系统架构](design/Forge-Agent-当前代码架构.md) ·
[按源码符号导航](source-navigation.md)

## 快速导航

| 主题 | 解决的问题 | 入口 |
|---|---|---|
| Agent Execution | 四入口如何汇合到 Runner，direct 与 isolate 如何分叉 | [任务执行主链](design/01-任务执行主链.md) |
| Tool Lifecycle & Security | ReAct、权限、Hooks、只读约束和 Completion Guard 如何协作 | [Agent 与 Tool](design/02-Agent与Tool生命周期.md) |
| Context Engineering | History、Token Budget、Repo Map、Compaction 和 Session 如何分工 | [Context 与 Repo Map](design/03-Context与RepoMap.md) |
| Planning & Recovery | typed plan、失败分类、恢复预算和 replan gate 如何运行 | [Planning 与 Recovery](design/04-Planning与Recovery.md) |
| Skills & MCP | progressive disclosure 和外部工具桥接的边界是什么 | [Skills 与 MCP](design/05-Skills与MCP.md) |
| Isolation & Acceptance | Worktree、Runtime、独立验收和 GitHub 交付如何隔离 | [隔离与独立验收](design/06-隔离与独立验收.md) |
| Evaluation & Evolution | 冻结评测与离线 Candidate promotion 如何保持证据边界 | [Evaluation 与 Evolution](design/07-Evaluation与Evolution.md) |

## 推荐阅读路径

- **理解整个项目**：整体架构 → 任务执行主链 → Agent/Tool → Context → Planning/Recovery →
  Skills/MCP → 隔离验收 → Evaluation/Evolution。
- **面试前快速复习**：从[面试复习入口](interview-review.md)开始，遇到类名或函数名时进入
  [源码导航](source-navigation.md)。

本目录把“当前事实”和“历史过程”分开。开始新任务时优先阅读 source of truth；只有需要追溯
设计决策、测试过程或实验原始证据时才进入历史日志。

## 本地网站使用

WSL 中一次性安装独立文档环境：

```bash
python3 -m venv ~/.venvs/forge-agent-docs
~/.venvs/forge-agent-docs/bin/pip install -r requirements-docs.txt
```

预览和构建：

```bash
bash scripts/serve_docs.sh
~/.venvs/forge-agent-docs/bin/python -m zensical build --clean --strict
```

静态输出位于 `site/`。offline 模式使用相对 HTML 链接与本地 worker polyfill，可直接打开
`site/index.html`；浏览器自动化验证边界记录在阶段二变更日志中。

## Source of truth

- [`TODO-P0-P1.md`](https://github.com/Napabana/forge-agent/blob/dev/TODO-P0-P1.md)：P0/P1 当前状态、真实剩余项和延期项。
- [`Forge-Agent-P0-P1-实施计划.md`](https://github.com/Napabana/forge-agent/blob/dev/Forge-Agent-P0-P1-%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92.md)：下一批实施顺序与验收原则。
- [`README.md`](https://github.com/Napabana/forge-agent/blob/dev/README.md)：项目能力、安装与使用入口。
- [`USAGE.md`](https://github.com/Napabana/forge-agent/blob/dev/USAGE.md)：详细运行配置和命令。

## 设计文档

架构知识库以当前源码为事实源，推荐按以下顺序阅读：

1. [`design/Forge-Agent-当前代码架构.md`](design/Forge-Agent-当前代码架构.md)：总架构、五条执行路径、任务生命周期与状态边界。
2. [`design/01-任务执行主链.md`](design/01-任务执行主链.md)：四入口、Runner、Orchestrator 和任务生命周期。
3. [`design/02-Agent与Tool生命周期.md`](design/02-Agent与Tool生命周期.md)：同步 ReAct、Tool lifecycle、Completion Guard 和 Trace。
4. [`design/03-Context与RepoMap.md`](design/03-Context与RepoMap.md)：History、Token Budget、Persistent Repo Map、Compaction 和 Session。
5. [`design/04-Planning与Recovery.md`](design/04-Planning与Recovery.md)：typed Planning、failure classification、Recovery budget 和 replan gate。
6. [`design/05-Skills与MCP.md`](design/05-Skills与MCP.md)：progressive disclosure、MCP client/adapter 与权限边界。
7. [`design/06-隔离与独立验收.md`](design/06-隔离与独立验收.md)：TaskEngine、Worktree、Docker、read-only、Acceptance 和 PR delivery。
8. [`design/07-Evaluation与Evolution.md`](design/07-Evaluation与Evolution.md)：Evaluation Harness、Benchmark 与离线 Candidate promotion gate。
9. [`design/源码索引.md`](design/源码索引.md)：功能→源码→测试和源码→功能→测试双向索引。
10. [`design/MAINTENANCE.md`](design/MAINTENANCE.md)：开发前后影响分析、文档同步与分级验证协议。

这些 Markdown 与其中的 Mermaid 是本地 HTML 文档站的唯一内容源；不要另行维护一套 HTML 正文。

## 历史计划与变更

历史计划、施工日志和证据只保留在本地，不进入公开 Git 仓库、站点导航与全文搜索，也不代表当前待办：

- `docs/plans/`：历史实施方案和归档计划。
- `docs/changes/`：按日期追加的开发记录。
- `docs/evidence/`：证据索引和可说/不可说边界。

## 评测证据

评测原始输出保持在 `evals/results/`，不移动到文档目录：

- `evals/results/context_policy_benchmark/report.json`：B1，7 cases × 3 variants 的离线 fixture benchmark。
- `evals/results/context_policy_agent_ablation_v2/report.json`：B2 v2 对照基线。
- [`evals/results/context_policy_agent_ablation_v3/report.md`](https://github.com/Napabana/forge-agent/blob/dev/evals/results/context_policy_agent_ablation_v3/report.md)：B2 v3 人类可读报告；同目录保留 `raw.jsonl`、`report.json` 和 traces。
- `evals/results/repo_map_ablation/report.json`：Repo Map retrieval 与性能消融。

## 阅读规则

- 判断“现在做什么”只看根目录 TODO 和实施计划。
- 历史日志中的“下一步”保留当时语境，不自动恢复为当前待办。
- fixture、单次真实案例和小样本 Agent 实验必须按各自证据边界表述，不外推总体成功率。
