"""
harness/executor.py

ToolExecutor —— 工具执行的统一包装层（M2 Task 2.2 / P0-2）。

把 Validation + Hooks + Permission + Tool 管线接到现有工具执行链路上，而不改动
各个 Tool 的实现。permission/hooks 为 None 时仍保持透明直通；生产入口由 Runner /
orchestrator 注入 PermissionManager。

冻结执行顺序：
    1. cooperative cancel 边界
    2. validate tool call
    3. PreToolUse hooks（block / failure 都不再进入 permission）
    4. cooperative cancel 边界
    5. PermissionManager.check → DENY / CONFIRM / ALLOW
    6. cooperative cancel 边界
    7. registry.execute_tool
    8. PostToolUse hooks（工具已经发生后只做观察/诊断，不覆盖真实 ToolResult）

同步 Tool 一旦开始执行，本层不会强制中断。cancel 在 Tool 执行期间到达时，仍完成
post-hook，并把真实 ToolResult 返回给 Agent；Agent 在下一安全边界进入 CANCELED。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from harness.hooks import HookEvent, Hooks
from harness.permission import PermissionDecision, PermissionManager, ToolUseBlock
from tools.base import ToolErrorType, ToolRegistry, ToolResult
from tools.shell_tool import ConfirmCallback

logger = logging.getLogger(__name__)

DecisionCallback = Callable[[str, dict[str, Any], PermissionDecision], None]


class ToolExecutionCanceled(RuntimeError):
    """Tool 尚未开始执行时，在 cooperative cancel 安全边界停止本次调用。"""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"tool execution canceled at {phase}")


class ToolExecutorInfrastructureError(RuntimeError):
    """Hook 外的 permission/confirm/framework 子系统故障。"""

    def __init__(self, phase: str, error: BaseException) -> None:
        self.phase = phase
        self.original_error = error
        super().__init__(f"{phase} failed: {type(error).__name__}: {error}")


class ToolExecutor:
    """包装 ToolRegistry，在执行前后接入 Hooks + Permission + cooperative cancel。"""

    def __init__(
        self,
        registry: ToolRegistry,
        permission: PermissionManager | None = None,
        hooks: Hooks | None = None,
        confirm_callback: ConfirmCallback | None = None,
        decision_callback: DecisionCallback | None = None,
        cancel_event: object | None = None,
    ) -> None:
        self._registry = registry
        self._permission = permission
        self._hooks = hooks
        self._confirm_callback = confirm_callback
        self._decision_callback = decision_callback
        self._cancel_event = cancel_event

    def execute(
        self,
        name: str,
        params: dict[str, Any],
        *,
        decision_callback: DecisionCallback | None = None,
        cancel_event: object | None = None,
    ) -> ToolResult:
        """按冻结生命周期执行一次工具调用。"""
        self._raise_if_canceled("before_validation", cancel_event)

        invalid = self._registry.validate_tool_call(name, params)
        if invalid is not None:
            return invalid
        block = ToolUseBlock(name, params)

        # 1. PreToolUse：明确 block 与 hook 自身异常都 fail-closed，但属于可恢复 Observation。
        if self._hooks is not None:
            try:
                blocked = self._hooks.trigger_pre_tool_use(block)
            except Exception as exc:
                if self._is_cancel_requested(cancel_event):
                    raise ToolExecutionCanceled("after_pre_hook") from exc
                reason = f"PreToolUse hook failed: {type(exc).__name__}: {exc}"
                logger.warning("[executor] %s", reason)
                return ToolResult(
                    success=False,
                    output="",
                    error=reason,
                    error_type=ToolErrorType.HOOK_FAILED,
                )

            self._raise_if_canceled("after_pre_hook", cancel_event)
            if blocked is not None:
                return ToolResult(
                    success=False,
                    output="",
                    error=blocked.reason,
                    error_type=ToolErrorType.HOOK_BLOCKED,
                )

        # 2. Permission：策略 deny 是正常结果；permission/confirm 子系统异常是基础设施错误。
        if self._permission is not None:
            self._raise_if_canceled("before_permission", cancel_event)
            try:
                decision = self._permission.check(block)
            except Exception as exc:
                if self._is_cancel_requested(cancel_event):
                    raise ToolExecutionCanceled("permission") from exc
                raise ToolExecutorInfrastructureError("permission", exc) from exc

            observer = self._decision_callback or decision_callback
            if observer is not None:
                try:
                    observer(block.name, dict(block.input), decision)
                except Exception as exc:  # noqa: BLE001 — 审计观察者不得改变执行结果
                    logger.warning("[executor] decision_callback error: %s", exc)

            self._raise_if_canceled("after_permission", cancel_event)
            if decision.is_deny:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Permission denied: {decision.reason}",
                    error_type=ToolErrorType.PERMISSION_DENIED,
                )
            if decision.is_confirm:
                try:
                    confirmed = self._confirm_prompt_ok(decision, block)
                except Exception as exc:
                    if self._is_cancel_requested(cancel_event):
                        raise ToolExecutionCanceled("permission_confirm") from exc
                    raise ToolExecutorInfrastructureError("permission_confirm", exc) from exc
                self._raise_if_canceled("after_permission", cancel_event)
                if not confirmed:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Permission denied by user: {decision.reason}",
                        error_type=ToolErrorType.PERMISSION_DENIED,
                    )

        # 3. Tool：这是最后一个能保证“Tool 尚未发生”的 cancel 边界。
        self._raise_if_canceled("before_tool", cancel_event)
        result = self._registry.execute_tool(name, params)
        if not result.success and result.error_type is None:
            result.error_type = _classify_tool_error(name, result)

        # 4. PostToolUse：Tool 已经发生，post-hook 失败只能作为 diagnostic，不能覆盖结果。
        cancel_after_tool = self._is_cancel_requested(cancel_event)
        if self._hooks is not None:
            try:
                self._hooks.trigger(HookEvent.POST_TOOL_USE, block, result)
            except Exception as exc:  # noqa: BLE001 — 观察钩子不能影响真实 ToolResult
                logger.warning("[executor] PostToolUse hook error: %s", exc)
                result.diagnostics += (f"post_tool_hook:{type(exc).__name__}",)

        if cancel_after_tool or self._is_cancel_requested(cancel_event):
            result.diagnostics += ("cancel_requested_after_tool",)
        return result

    def _confirm_prompt_ok(self, decision: PermissionDecision, block: ToolUseBlock) -> bool:
        """CONFIRM 交给注入 callback；无 callback 是正常 deny，callback 抛错由上层判 infra。"""
        if self._confirm_callback is None:
            return False
        prompt = block.input.get("cmd") or block.input.get("command") or block.name
        return bool(self._confirm_callback(prompt))

    def _is_cancel_requested(self, cancel_event: object | None = None) -> bool:
        event = cancel_event if cancel_event is not None else self._cancel_event
        if event is None:
            return False
        is_set = getattr(event, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        return bool(event)

    def _raise_if_canceled(self, phase: str, cancel_event: object | None = None) -> None:
        if self._is_cancel_requested(cancel_event):
            raise ToolExecutionCanceled(phase)


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
