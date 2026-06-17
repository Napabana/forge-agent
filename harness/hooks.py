"""
harness/hooks.py

工具执行钩子（M2 Task 2.2）。

把 s20_comprehensive/code.py 的全局 Hooks 机制（HOOKS dict + register_hook +
trigger_hooks，code.py:860-873）迁移成实例化的 Hooks 类。语义不变：
trigger 遇到首个非 None 返回即短路（拦截）。

事件类型对齐 s20：UserPromptSubmit / PreToolUse / PostToolUse / Stop。
"""

from __future__ import annotations

from typing import Any, Callable


# ---------------------------------------------------------------------------
# 事件类型（对齐 s20 HOOKS 的 key）
# ---------------------------------------------------------------------------

class HookEvent:
    PRE_TOOL_USE = "PreToolUse"            # 工具执行前（可拦截，收 ToolUseBlock）
    POST_TOOL_USE = "PostToolUse"          # 工具执行后（收 ToolUseBlock, ToolResult）
    USER_PROMPT_SUBMIT = "UserPromptSubmit"  # 用户提交 prompt（收 query str）
    STOP = "Stop"                          # agent 停止（收 messages）


HookCallback = Callable[..., Any]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

class Hooks:
    """
    钩子注册表。实例化（替代 s20 的全局 HOOKS dict）。

    用法：
        hooks = Hooks()
        hooks.register(HookEvent.PRE_TOOL_USE, my_hook)
        result = hooks.trigger(HookEvent.PRE_TOOL_USE, block)  # 非 None 即拦截
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {
            HookEvent.PRE_TOOL_USE: [],
            HookEvent.POST_TOOL_USE: [],
            HookEvent.USER_PROMPT_SUBMIT: [],
            HookEvent.STOP: [],
        }

    def register(self, event: str, callback: HookCallback) -> "Hooks":
        """注册钩子。支持链式。未知事件名会抛 ValueError。"""
        if event not in self._hooks:
            raise ValueError(f"Unknown hook event: {event!r}")
        self._hooks[event].append(callback)
        return self

    def unregister(self, event: str, callback: HookCallback) -> bool:
        subs = self._hooks.get(event, [])
        try:
            subs.remove(callback)
            return True
        except ValueError:
            return False

    def trigger(self, event: str, *args: Any) -> Any:
        """
        触发某事件的所有钩子，按注册顺序执行。
        遇到首个返回非 None 的钩子即短路，返回该值（对齐 s20 trigger_hooks:868-873）。
        全部返回 None 则返回 None（放行）。
        """
        for callback in self._hooks.get(event, []):
            result = callback(*args)
            if result is not None:
                return result
        return None

    def count(self, event: str) -> int:
        return len(self._hooks.get(event, []))
