"""harness/ — 工具执行的安全与可观测管线（M2 Task 2.2 / P0-2）。

包级 ``ToolExecutor`` 是产品 Runner 使用的安全默认：未显式传 PermissionManager 时
自动启用默认策略。需要测试底层透明直通时，可直接从 ``harness.executor`` 导入原始
ToolExecutor；Agent 的低层默认路径也保持这种透明语义。
"""

from harness.executor import (
    ToolExecutionCanceled,
    ToolExecutor as _RawToolExecutor,
    ToolExecutorInfrastructureError,
)
from harness.hooks import HookBlockResult, HookEvent, Hooks
from harness.permission import (
    ALLOW,
    Decision,
    PermissionDecision,
    PermissionManager,
    ToolUseBlock,
)


class ToolExecutor(_RawToolExecutor):
    """生产组合根使用的 ToolExecutor：默认启用 PermissionManager。"""

    def __init__(
        self,
        registry,
        permission=None,
        hooks=None,
        confirm_callback=None,
        decision_callback=None,
        cancel_event=None,
    ) -> None:
        super().__init__(
            registry,
            permission=permission or PermissionManager(registry=registry),
            hooks=hooks,
            confirm_callback=confirm_callback,
            decision_callback=decision_callback,
            cancel_event=cancel_event,
        )


__all__ = [
    "ToolExecutor",
    "ToolExecutionCanceled",
    "ToolExecutorInfrastructureError",
    "Hooks",
    "HookEvent",
    "HookBlockResult",
    "PermissionManager",
    "PermissionDecision",
    "Decision",
    "ToolUseBlock",
    "ALLOW",
]
