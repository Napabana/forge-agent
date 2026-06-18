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
from tools.base import ToolRegistry, ToolResult
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
        block = ToolUseBlock(name, params)

        # 1. PreToolUse hooks（短路）
        if self._hooks is not None:
            blocked = self._hooks.trigger(HookEvent.PRE_TOOL_USE, block)
            if blocked is not None:
                return ToolResult(success=False, output="", error=str(blocked))

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
                return ToolResult(success=False, output="",
                                  error=f"Permission denied: {decision.reason}")
            if decision.is_confirm:
                if not self._confirm_prompt_ok(decision, block):
                    return ToolResult(success=False, output="",
                                      error=f"Permission denied by user: {decision.reason}")

        # 3. 执行底层工具
        result = self._registry.execute_tool(name, params)

        # 4. PostToolUse hooks（不拦截，仅观察）
        if self._hooks is not None:
            try:
                self._hooks.trigger(HookEvent.POST_TOOL_USE, block, result)
            except Exception as exc:  # noqa: BLE001  观察钩子不能影响主流程
                logger.warning("[executor] PostToolUse hook error: %s", exc)

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
