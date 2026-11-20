# 任务执行

Run、Chat 和 Issue 将事件观察者和流式回调接入 [CLI 终端界面](terminal-ui.md)。

## 为什么实现

ExecutionRunner 为入口提供统一生命周期，使同一个任务在交互、自动化和评测中使用同一套 Agent 与工具。

## 怎么使用

使用 `forgeAgent run --repo . --task '描述任务'` 启动一次运行，或用 `forgeAgent chat --repo .` 进入交互会话。评测参数可用 `python -m evals.coding_agent --help` 查阅。

## 怎样实现

`RunRequest` 携带任务、隔离策略、约束和验收合同。Runner 加载配置和仓库身份，构造 Agent 或工作树编排器。任务状态和 Trace 通过 `RunResult` 返回。隔离工作使用 `TaskEngine` 原子认领任务并更新状态，`AgentBus` 负责进程内事件分发。

每条 Chat 消息通过同一个 Runner 执行独立轮次。会话可复用 Agent 和上下文，每轮根据当前审批配置和执行约束绑定工具执行器。

CLI 中用 `forgeAgent log show PATH` 查看已保存的运行摘要与事件。

## 预期效果

入口保持一致，调用方能分别读出运行结果、工作树产物和验收结果。

## 使用边界

取消和暂停在安全执行边界生效。任务认领、运行时检查点和工作树的生命周期各有独立记录。
