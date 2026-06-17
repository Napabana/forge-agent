"""harness/ — 工具执行的安全与可观测管线（M2 Task 2.2）。

Hooks + Permission + ToolExecutor，移植并现代化自 s20 的 Hooks/Permission。
"""

from harness.executor import ToolExecutor
from harness.hooks import HookEvent, Hooks
from harness.permission import (
    ALLOW,
    Decision,
    PermissionDecision,
    PermissionManager,
    ToolUseBlock,
)

__all__ = [
    "ToolExecutor",
    "Hooks",
    "HookEvent",
    "PermissionManager",
    "PermissionDecision",
    "Decision",
    "ToolUseBlock",
    "ALLOW",
]
