# Agent 执行

将任务编排为模型调用、工具观察和独立验收。

## 怎么使用

从 CLI 等入口创建 `RunRequest` 并交给 `ExecutionRunner`。

## 从哪里读代码

- [`core.py`](core.py)
- [`runner.py`](runner.py)
- [`orchestrate.py`](orchestrate.py)
- [`coordinator.py`](coordinator.py)
- [`background_workers.py`](background_workers.py)
- [`event_log.py`](event_log.py)

## 预期效果

CLI 和评测复用同一执行循环。模型选择动作、工具返回观察、完成条件检查都有对应运行事件，便于查明任务停在哪一步。

启用计划后，可先读代码和搜索。Test 工具的红测会留下测试未通过的证据，完成门禁继续要求通过的测试。

启用 Worker 后，Coordinator 管理整个运行的委派额度和各 Worker 的分配。执行循环在请求前检查 Worker 的剩余额度，并向主 Agent 送达已完成或停止的 Worker 摘要。完整结果仍可按任务 ID 领取。

## 设计文档

[任务执行](../docs/design/execution.md) · [Worker 协作](../docs/design/workers.md)
