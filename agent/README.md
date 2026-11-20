# Agent 执行

将任务编排为模型调用、工具观察和独立验收。

## 怎么使用

从 CLI 等入口创建 `RunRequest` 并交给 `ExecutionRunner`。

## 从哪里读代码

- [`core.py`](core.py)
- [`runner.py`](runner.py)
- [`orchestrate.py`](orchestrate.py)
- [`coordinator.py`](coordinator.py)
- [`event_log.py`](event_log.py)

## 预期效果

CLI 和评测复用同一执行循环。模型选择动作、工具返回观察、完成条件检查都有对应运行事件，便于查明任务停在哪一步。

## 设计文档

[任务执行](../docs/design/execution.md)
