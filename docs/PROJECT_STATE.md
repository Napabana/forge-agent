# 当前项目状态

Forge Agent 提供命令行和评测入口。CLI 支持单次运行、交互 Chat、任务恢复和 GitHub Issue 交付。核心能力包括可校验的工具执行、隔离工作树、受监督 Worker、项目记忆及可持久化的任务恢复。

| 能力 | 当前入口 |
| --- | --- |
| 单次和交互编码 | `forgeAgent run`、`forgeAgent chat` |
| 终端显示 | [紧凑界面、流式输出、模型选择和 Chat 审批切换](design/terminal-ui.md) |
| 持久化和恢复 | `forgeAgent runtime`、Chat `/resume` 与 `/rewind` |
| 归档共享 | 同项目内的 `archive-export` 与 `archive-import` |
| 独立验收与交付 | 路径契约、Verifier、`python -m entry.github_issue` |
| 评测 | `python -m evals.coding_agent`、`python -m evals.long_horizon` |

## 验证与边界

Chat 的 `/approval` 复用 Run 的审批语义，切换在当前进程中用于后续轮次。WSL 已用原 `forgeAgent` 命令验证三种模式和 `/status`。本轮在 WSL 源码快照运行 `python -m pytest -q -p no:cacheprovider`，结果为 1,435 项通过、21 项跳过、0 项失败。共享历史预检接入同一仓库快照后，主工作树专项复测 147 项通过。文档一致性通过 `python scripts/check_docs.py --validate` 校验。

Chat 在每轮开始共享仓库状态，普通只读步骤跳过重复的内容指纹扫描。编辑同步到下一次 RepoMap 请求，验收和恢复继续校验工作区。在 WSL 原生文件系统上的 `pr-test`（`2301999`），使用暖缓存对两个离线场景各测七轮。纯问答中位数从 66.27 ms 降到 30.41 ms，单次读取源码从 103.79 ms 降到 45.47 ms。外部重复编辑同一路径和工具写入均验证了 RepoMap 更新。两轮只读真实模型测试均完成，`pr-test` 自有测试 68 项通过。大型挂载盘工作区的冷索引耗时仍需单独测量。

当前实现的使用和验收范围见 [整体架构](design/architecture.md) 与 [长时运行与恢复](design/durable-runtime.md)。
