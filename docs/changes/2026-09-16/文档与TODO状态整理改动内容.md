# 文档与 TODO 状态整理改动内容

日期：2026-09-16  
基线：`dev@ba0720d3ca909afd0146510bd5bad7cf8131d2d6`

## 目标

本轮只对齐 TODO/实施计划与当前仓库证据，并整理 Markdown 目录；不修改生产代码或测试代码，
不运行付费模型，不修改 `config/default.yaml`、remote 或 stash，不 commit/push/rebase。

## 状态审计结论

- P0-1、P1-1、P1-2、P1-3、P1-5 核心标记为 `DONE`。
- P0-2、P0-3、P1-4、P1-6 保持 `PARTIAL`，并列出可验证的剩余工作。
- Context Compaction C1-C5、B1、B2 标记为 `DONE`；C6 `context_recall(event_ref)` 为
  `DEFERRED`。
- Repo Map 的 query-aware ranking、文件工具写后刷新、正式 retrieval/performance 消融属于
  已完成核心；Git/working-tree cache identity、shell/git 删除/重命名感知和更大 Agent 对照
  单列为边界或可选证据。
- B1 fixture、B2 `n=3` 单次真实 Agent run、真实 PR 单案例和 Repo Map 固定任务消融均保留
  各自外推限制，没有把局部证据写成总体成功率。

## 文档结构变化

- 根目录保留 5 个长期入口：`AGENTS.md`、`README.md`、`USAGE.md`、`TODO-P0-P1.md`、
  `Forge-Agent-P0-P1-实施计划.md`。
- 5 份 2026-09-14 日志迁入 `docs/changes/2026-09-14/`。
- 26 份 2026-09-15 日志迁入 `docs/changes/2026-09-15/`。
- 原有 3 份 2026-09-16 B2 文档迁入 `docs/changes/2026-09-16/`；本文件为该目录第 4 份记录。
- 当前架构文档迁入 `docs/design/Forge-Agent-当前代码架构.md`。
- 两份 Context Compaction 历史计划与两份状态对齐前的长文快照保存在
  `docs/plans/archive/`，未删除历史。
- 新增 `docs/README.md`，统一导航 source of truth、设计、历史计划、日期日志和评测证据。
- 已跟踪 Markdown 使用 Windows Git `git mv`；本地忽略 Markdown 通过
  `Ubuntu-22.04-Recovered` 移动。

## 主要内容更新

- `TODO-P0-P1.md`：改为 `DONE/PARTIAL/TODO/DEFERRED` 当前状态账本。
- `Forge-Agent-P0-P1-实施计划.md`：改为已完成主线、Batch A-E、证据基线和非目标。
- `AGENTS.md`：固定 `Ubuntu-22.04-Recovered`，更新 HEAD、当前状态、下一步和最后交接。
- 历史 Compaction 计划增加归档标识；移动后的相关路径引用已更新。

## 验证

- 未运行 Python/pytest：本轮没有任何生产代码或测试代码变更。
- 执行 Markdown 本地链接检查：共检查 75 份 Markdown，0 个失效本地链接。
- 执行根目录 Markdown、日期目录数量和旧文件名引用扫描。
- 执行 Windows Git `status`、`diff --stat`、`diff --check` 与变更路径检查；`diff --check`
  无空白错误，仅显示 Windows Git 对既有 LF 文件的 CRLF 提示。

## 已知边界

- `.gitignore` 继续忽略一般 `*.md`；本轮不改变该仓库约定，也不强制添加本地 Markdown。
- eval fixture 内已有嵌套仓库状态原样保留，不属于本轮清理范围。
- 历史日志保留当时的结论和上下文；当前优先级只以根目录 TODO/实施计划为准。
- 本轮不实施下一批 P0/P1 生产代码。
