"""按事件实例化注册工具执行钩子。

触发时按注册顺序运行，并返回第一个非空结果。钩子把扩展检查与工具执行流程分开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 支持的钩子事件
# ---------------------------------------------------------------------------

class HookEvent:
    PRE_TOOL_USE = "PreToolUse"            # 工具执行前（可拦截，收 ToolUseBlock）
    POST_TOOL_USE = "PostToolUse"          # 工具执行后（收 ToolUseBlock, ToolResult）
    USER_PROMPT_SUBMIT = "UserPromptSubmit"  # 用户提交 prompt（收 query str）
    STOP = "Stop"                          # agent 停止（收 messages）


HookCallback = Callable[..., Any]


@dataclass(frozen=True)
class HookBlockResult:
    """PreToolUse 明确拒绝执行时返回的结果。"""

    reason: str


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

class Hooks:
    """
    实例级钩子注册表。

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

    def trigger_pre_tool_use(self, block: Any) -> HookBlockResult | None:
        """触发 PreToolUse，并统一转换为类型化的阻止结果。"""

        result = self.trigger(HookEvent.PRE_TOOL_USE, block)
        if result is None:
            return None
        if isinstance(result, HookBlockResult):
            return result

        # 兼容旧 Hook 返回字符串或其他非 None 值的行为。
        return HookBlockResult(reason=str(result))

    def trigger(self, event: str, *args: Any) -> Any:
        """
        触发某事件的所有钩子，按注册顺序执行。
        返回首个非空钩子结果。
        全部返回 None 则返回 None（放行）。

        主要负责：
        根据 HookEvent.PRE_TOOL_USE 找到对应 Hook；
        按注册顺序调用这些 Hook；
        将 block 传给 Hook；
        如果某个 Hook 返回非 None，把该结果返回给调用方；
        如果所有 Hook 都允许执行，返回 None。
        """
        for callback in self._hooks.get(event, []):
            result = callback(*args)
            if result is not None:
                return result
        return None

    def count(self, event: str) -> int:
        return len(self._hooks.get(event, []))
