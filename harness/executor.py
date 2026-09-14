"""
harness/executor.py

ToolExecutor —— 工具执行的统一包装层（M2 Task 2.2）。

把 Hooks + Permission 管线接到现有工具执行链路上，而不改动各个 Tool 的实现。
与 tools/base.py 的 ToolRegistry.execute_tool 同签名：(name, params) -> ToolResult，
返回 ToolResult，兼容 result.to_observation()，因此 agent/core.py 可无缝替换。

执行流程：
    1. 构造 ToolUseBlock(name, params)
    2. PreToolUse hooks（短路拦截：返回非 None 即拒绝）
    3. PermissionManager.check → DENY 拒绝 / CONFIRM 走 confirm_callback / ALLOW 放行
    4. registry.execute_tool 执行底层工具
    5. PostToolUse hooks（观察，不拦截）

设计意图：permission/hooks 为 None 时是「透明直通」，行为等价于直接调
registry.execute_tool —— 保证既有调用方零回归。安全管线按需注入。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from harness.hooks import HookEvent, Hooks
from harness.permission import PermissionDecision, PermissionManager, ToolUseBlock
from tools.base import ToolErrorType, ToolRegistry, ToolResult
from tools.shell_tool import ConfirmCallback

logger = logging.getLogger(__name__)


# 权限决策观察回调：(tool_name, params_copy, decision) -> None。
# 在 permission.check 之后、分支处理之前同步触发，allow/deny/confirm 都上报。
# 供 orchestrator 记 PERMISSION_DECISION 事件 / 转发 AgentBus。
DecisionCallback = "Callable[[str, dict[str, Any], PermissionDecision], None]"


class ToolExecutor:
    """
    包装 ToolRegistry，在执行前后接入 Hooks + Permission。

    Args:
        registry:          被包装的工具注册表
        permission:        权限校验器；None=跳过权限校验
        hooks:             钩子集合；None=不触发任何钩子
        confirm_callback:  CONFIRM 决策时调用，返回 False=拒绝；None=CONFIRM 一律拒绝
        decision_callback: 权限决策观察回调（M4）；None=不上报（默认，零回归）
    """

    def __init__(
        self,
        registry: ToolRegistry,
        permission: PermissionManager | None = None,
        hooks: Hooks | None = None,
        confirm_callback: ConfirmCallback | None = None,
        decision_callback: Callable[[str, dict[str, Any], PermissionDecision], None] | None = None,
    ) -> None:
        self._registry = registry
        self._permission = permission
        self._hooks = hooks
        self._confirm_callback = confirm_callback
        self._decision_callback = decision_callback

    # ------------------------------------------------------------------
    # 与 ToolRegistry.execute_tool 同签名
    # ------------------------------------------------------------------

    def execute(self, name: str, params: dict[str, Any]) -> ToolResult:
        """Run hooks, permission checks, and the underlying tool."""
        invalid = self._registry.validate_tool_call(name, params)
        if invalid is not None:
            return invalid
        block = ToolUseBlock(name, params)

        # 1. PreToolUse hooks（异常或明确拒绝都 fail-closed）
        if self._hooks is not None:
            try:
                blocked = self._hooks.trigger_pre_tool_use(block)
            except Exception as exc:
                reason = f"PreToolUse hook failed: {type(exc).__name__}: {exc}"
                logger.warning("[executor] %s", reason)
                return ToolResult(
                    success=False,
                    output="",
                    error=reason,
                    error_type=ToolErrorType.HOOK_FAILED,
                )

            if blocked is not None:
                return ToolResult(
                    success=False,
                    output="",
                    error=blocked.reason,
                    error_type=ToolErrorType.HOOK_BLOCKED,
                )
        # 2. Permission
        if self._permission is not None:
            decision = self._permission.check(block)

            # 观察决策（allow/deny/confirm 都上报），在分支处理之前，
            # 这样被 DENY/CONFIRM-拒绝的调用也能被记录（供审计/bus）。
            if self._decision_callback is not None:
                try:
                    self._decision_callback(block.name, dict(block.input), decision)
                except Exception as exc:  # noqa: BLE001 — 观察者不能影响执行
                    logger.warning("[executor] decision_callback error: %s", exc)

            if decision.is_deny:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Permission denied: {decision.reason}",
                    error_type=ToolErrorType.PERMISSION_DENIED,
                )
            if decision.is_confirm:
                if not self._confirm_prompt_ok(decision, block):
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Permission denied by user: {decision.reason}",
                        error_type=ToolErrorType.PERMISSION_DENIED,
                    )

        # 3. 执行底层工具
        result = self._registry.execute_tool(name, params)
        if not result.success and result.error_type is None:
            result.error_type = _classify_tool_error(name, result)

        # 4. PostToolUse hooks（不拦截，仅观察）
        if self._hooks is not None:
            try:
                self._hooks.trigger(HookEvent.POST_TOOL_USE, block, result)
            except Exception as exc:  # noqa: BLE001  观察钩子不能影响主流程
                logger.warning("[executor] PostToolUse hook error: %s", exc)
                result.diagnostics += (f"post_tool_hook:{type(exc).__name__}",)

        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _confirm_prompt_ok(self, decision: PermissionDecision, block: ToolUseBlock) -> bool:
        """CONFIRM 决策交给注入的 callback。无 callback 时默认拒绝（安全优先）。"""
        if self._confirm_callback is None:
            return False
        # ConfirmCallback 签名是 (cmd: str) -> bool；shell 用命令，其它用工具名
        prompt = block.input.get("cmd") or block.input.get("command") or block.name
        try:
            return bool(self._confirm_callback(prompt))
        except Exception:  # noqa: BLE001
            logger.warning("[executor] confirm_callback raised; denying")
            return False


def _classify_tool_error(name: str, result: ToolResult) -> ToolErrorType:
    text = "\n".join(part for part in (result.output, result.error) if part).lower()
    if "timed out" in text or "timeout" in text:
        return ToolErrorType.TIMEOUT
    if name in {"shell", "test", "pytest", "git"} and any(marker in text for marker in (
        "cannot connect to the docker daemon",
        "docker is not available",
        "failed to start container",
        "duplicate mount point",
        "invalid mount config",
    )):
        return ToolErrorType.INFRASTRUCTURE
    return ToolErrorType.TOOL_EXECUTION
