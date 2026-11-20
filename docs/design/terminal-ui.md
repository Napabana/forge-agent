# CLI 终端界面

Run、隔离 Run、Chat 和 GitHub Issue 使用同一套终端组件。普通终端在当前滚动历史中留下任务信息、工具结果和回答。重定向、CI 和 `TERM=dumb` 使用纯文本。CLI 会在强制 Rich 的终端能力不足时向 stderr 说明降级原因。

## 显示与执行

`entry/cli.py` 负责读取 UI 配置并把观察者、回答流和显式开启的推理流接到 `entry/ui/terminal.py`。`entry/ui/presenter.py` 按 EventType 投影只读事件，选择允许显示的字段，并复用 Trace 脱敏规则。每个 Run 或 Chat 轮次独立记录已显示的事件、模型回答和工具结果。执行器继续写原有 JSONL。

Rich Console 负责状态行、Markdown、代码和 Diff。局部 Live 按配置频率刷新，结束后将内容写入滚动历史。用户确认、回到输入、异常退出时先撤下动态区域。权限回调仍负责实际决定。Chat 使用 prompt_toolkit 处理历史、补全、多行输入和外部编辑器。

## 查看执行结果

Run 结束后，`entry/ui/summary.py` 从 RunResult 读取 Agent 终态、验收、交付、用量和工作树处置，从观察事件统计工具结果。文件统计使用结果自带的 Git patch。Chat 的 `/diff` 展示最近一轮返回的工作区快照，它可能包含该轮开始前已有的改动。会话切换会清空这份临时视图。上下文用量以最近有记录的模型请求为准。

## 配置与验收

在用户 YAML 的 `ui` 段设置 `mode`（auto、rich、plain）、`theme`（default、dark、light、ansi）、`show_status_line`、`show_model_metadata`、`show_token_usage`、`markdown_enabled` 和 `animations`。动画默认关闭。工具输出使用 `tool_output_mode`（on_error、summary、all、none）和正整数 `tool_output_preview_lines`，默认 4 行。动态刷新使用 `live_refresh_per_second`，范围 1 至 10，默认 6。

组件测试覆盖 40、60、80、120 列布局和权限提示前后的光标恢复。`tests/test_run_terminal_ui.py` 使用 MockBackend 检查执行结果、原始 Trace 与终端输出，并在 Linux 伪终端运行 Rich CLI。`tests/test_chat_terminal_ui.py` 覆盖跨轮输出、会话切换、命令、编辑器绑定和多行历史。

设计合同保留了实施前的源码基线、方案示意和验收清单。当前实现的源码与命令以本页和命令帮助为准。[CLI Terminal UI 设计合同](12-CLI-Terminal-UI-架构.md)
