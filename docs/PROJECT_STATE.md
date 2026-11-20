# 当前项目状态

Forge Agent 提供命令行和评测入口。CLI 支持单次运行、交互 Chat、任务恢复和 GitHub Issue 交付。核心能力包括可校验的工具执行、隔离工作树、受监督 Worker、项目记忆及可持久化的任务恢复。

| 能力 | 当前入口 |
| --- | --- |
| 单次和交互编码 | `forgeAgent run`、`forgeAgent chat` |
| 终端显示 | [紧凑界面、流式输出、斜杠命令和可搜索模型选择](design/terminal-ui.md) |
| 持久化和恢复 | `forgeAgent runtime`、Chat `/resume` 与 `/rewind` |
| 归档共享 | 同项目内的 `archive-export` 与 `archive-import` |
| 独立验收与交付 | 路径契约、Verifier、`python -m entry.github_issue` |
| 评测 | `python -m evals.coding_agent`、`python -m evals.long_horizon` |

## 验证与边界

当前实现的使用和验收范围见 [整体架构](design/architecture.md) 与 [长时运行与恢复](design/durable-runtime.md)。
