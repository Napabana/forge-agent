"""
harness/permission.py

工具权限校验（M2 Task 2.2）。

把 s20_comprehensive/code.py 的 permission_hook（code.py:880-905）迁移成
实例化的 PermissionManager。语义保留：DENY 黑名单硬拦截 / DESTRUCTIVE 需确认 /
文件路径越界拒绝 / MCP deploy 需确认。

与 s20 的差异：
- 决策结构化（PermissionDecision：ALLOW / DENY / CONFIRM），替代 s20 的
  None/字符串二态——更清晰区分「需确认」与「允许」。
- 不在内部做交互式 input()（s20 在 hook 里 input，测试不友好）。CONFIRM 决策
  交由 harness/executor.py 的 ToolExecutor 用注入的 ConfirmCallback 处理。
- 黑名单/确认词复用 tools/shell_tool.py 已有常量，不重复定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# 复用 shell_tool 已有的安全常量（不重复定义）
from tools.shell_tool import (
    _BLOCKED_PATTERNS,
    _CONFIRM_KEYWORDS,
)


# ---------------------------------------------------------------------------
# 工具调用载体（对齐 s20 permission_hook 的 block.name / block.input）
# ---------------------------------------------------------------------------

@dataclass
class ToolUseBlock:
    """一次工具调用的结构化表示，喂给 permission / hooks。"""
    name: str           # 工具名，如 "shell" / "file_write" / "mcp__deploy__trigger"
    input: dict         # 工具参数


# ---------------------------------------------------------------------------
# 权限决策
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass
class PermissionDecision:
    """PermissionManager.check 的返回值。"""
    decision: Decision
    reason: str = ""

    @property
    def is_allow(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def is_deny(self) -> bool:
        return self.decision is Decision.DENY

    @property
    def is_confirm(self) -> bool:
        return self.decision is Decision.CONFIRM


# 便捷构造
ALLOW = PermissionDecision(Decision.ALLOW)


def _deny(reason: str) -> PermissionDecision:
    return PermissionDecision(Decision.DENY, reason)


def _confirm(prompt: str) -> PermissionDecision:
    return PermissionDecision(Decision.CONFIRM, prompt)


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------

class PermissionManager:
    """
    工具调用权限校验器。移植自 s20 permission_hook:880-905。

    构造参数：
        deny_patterns:    硬拦截黑名单（命令子串匹配）。默认合并 shell_tool
                          _BLOCKED_PATTERNS + s20 DENY_LIST 的补充项。
        confirm_keywords: 需确认的危险关键词。默认复用 shell_tool _CONFIRM_KEYWORDS。
        workspace:        允许写操作的根目录，文件路径越界即拒绝。None=不校验路径。
    """

    # s20 DENY_LIST 里 shell_tool 未覆盖的补充项（code.py:876）
    _EXTRA_DENY = ("sudo", "shutdown", "reboot")

    def __init__(
        self,
        deny_patterns: tuple[str, ...] | None = None,
        confirm_keywords: tuple[str, ...] | None = None,
        workspace: str | None = None,
    ) -> None:
        extra = tuple(p for p in self._EXTRA_DENY if p not in _BLOCKED_PATTERNS)
        self.deny_patterns = deny_patterns if deny_patterns is not None \
            else (_BLOCKED_PATTERNS + extra)
        self.confirm_keywords = confirm_keywords if confirm_keywords is not None \
            else _CONFIRM_KEYWORDS
        self.workspace = workspace

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def check(self, block: ToolUseBlock) -> PermissionDecision:
        """
        校验一次工具调用。
        shell 工具：黑名单 DENY；危险关键词 CONFIRM；否则 ALLOW。
        文件写工具：路径越界 DENY。
        MCP deploy 类工具：CONFIRM。
        其它：ALLOW。
        """
        # shell 命令
        if block.name in ("shell", "bash"):
            return self._check_command(block.input.get("cmd")
                                       or block.input.get("command") or "")

        # 文件写：路径越界
        if block.name in ("file_write", "file_edit", "edit", "write_file", "edit_file"):
            path = block.input.get("path", "")
            if self.workspace and path:
                reason = _path_escape_reason(path, self.workspace)
                if reason:
                    return _deny(reason)
            return ALLOW

        # MCP deploy 类
        if block.name.startswith("mcp__") and "deploy" in block.name:
            return _confirm(f"MCP destructive-looking tool: {block.name}")

        return ALLOW

    def _check_command(self, command: str) -> PermissionDecision:
        cmd_lower = command.lower()
        for pattern in self.deny_patterns:
            if pattern.lower() in cmd_lower:
                return _deny(f"'{pattern}' is on the deny list")
        for kw in self.confirm_keywords:
            if kw in cmd_lower:
                return _confirm(f"destructive command: {command}")
        return ALLOW


def _path_escape_reason(path: str, workspace: str) -> str:
    """
    路径是否逃逸 workspace（移植 s20 safe_path 的意图）。
    返回空串表示安全，非空为拒绝原因。
    """
    import os
    ws = os.path.abspath(workspace)
    try:
        target = os.path.abspath(os.path.join(ws, path)) if not os.path.isabs(path) \
            else os.path.abspath(path)
    except Exception as e:  # noqa: BLE001
        return f"invalid path {path!r}: {e}"
    if target == ws or target.startswith(ws + os.sep):
        return ""
    return f"path escapes workspace: {path}"
