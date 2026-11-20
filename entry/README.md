# 产品入口

将命令行任务、Chat 消息和 GitHub Issue 转成 Runner 可执行任务。

## 怎么使用

安装后用 `forgeAgent --help` 查看命令。使用 `forgeAgent chat` 进入交互会话，使用 `python -m entry.github_issue --help` 查阅 GitHub 交付参数。

## 从哪里读代码

- [`cli.py`](cli.py)
- [`chat.py`](chat.py)
- [`github_issue.py`](github_issue.py)
- [`onboarding.py`](onboarding.py)

## 预期效果

终端命令、会话消息和 GitHub Issue 进入同一执行链，在终端显示事件、任务状态和交付结果。

## 设计文档

[任务执行](../docs/design/execution.md)
