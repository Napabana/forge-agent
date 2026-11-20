# 工具执行控制

在实际调用前实施 Hook、权限、只读约束和取消边界，并记录结果。

## 怎么使用

通过 Runner 调用工具，集成时用 `ToolExecutor` 和已注册的 `ToolRegistry`。

## 从哪里读代码

- [`executor.py`](executor.py)
- [`permission.py`](permission.py)
- [`hooks.py`](hooks.py)

## 预期效果

工具调用统一经过 Hook、权限与执行约束检查。取消、失败和成功都有结构化结果，便于追查动作是否执行。

## 设计文档

[Agent 与工具执行](../docs/design/agent-tools.md)
