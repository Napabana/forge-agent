"""检查工具执行权限、工作区范围和工具副作用。

返回结构化的允许、拒绝或确认决策。需要用户确认时由执行器调用入口提供的回调。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

# 复用 shell_tool 已有的安全常量（不重复定义）
from tools.shell_tool import (
    _BLOCKED_PATTERNS,
    _CONFIRM_KEYWORDS,
    _is_repository_readonly,
)
from tools.base import ToolEffect, ToolRegistry


# ---------------------------------------------------------------------------
# 待检查的工具调用
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


@dataclass(frozen=True)
class ExecutionConstraints:
    """可信入口提供的运行约束；不从模型文本或评测标签推断。"""

    read_only: bool = False

    def __post_init__(self) -> None:
        if type(self.read_only) is not bool:
            raise ValueError("execution_constraints.read_only must be a boolean")

    @classmethod
    def from_dict(cls, raw: dict) -> "ExecutionConstraints":
        if not isinstance(raw, dict) or set(raw) - {"read_only"}:
            raise ValueError("invalid execution_constraints object")
        return cls(**raw)


_STRICT_READ_TOOLS = frozenset({
    "file_read", "file_view", "git_status", "git_diff",
    "search_text", "find_files", "find_symbol", "delegate_tasks",
})


def read_only_allows(block: ToolUseBlock) -> bool:
    """严格白名单；ToolEffect.READ_ONLY 不足以证明测试或 MCP 无副作用。"""
    if block.name in {"shell", "bash"}:
        # shortcut: 只接受固定查询命令，需要更多 Shell 时先审查参数及组合语义。
        return block.input.get("cmd", block.input.get("command")) in {
            "pwd", "ls", "ls -a", "ls -l", "ls -la",
        }
    return block.name in _STRICT_READ_TOOLS


# 受 workspace 路径边界约束的文件工具（读+写）。
# 读取工具同样遵循工作区边界。
# 含别名兼容（file_read/file_view/file_edit/file_write 是 forge-agent 的工具名；
# read_file/write_file/edit 等也按文件工具处理）。
_PATH_BOUND_TOOLS = frozenset({
    # forge-agent 工具
    "file_read", "file_view", "file_edit", "file_write",
    # 文件工具别名
    "read_file", "view_file", "write_file", "edit_file", "edit",
})


def _deny(reason: str) -> PermissionDecision:
    return PermissionDecision(Decision.DENY, reason)


def _confirm(prompt: str) -> PermissionDecision:
    return PermissionDecision(Decision.CONFIRM, prompt)


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------

class PermissionManager:
    """
    工具调用权限校验器。

    构造参数：
        deny_patterns:    硬拦截黑名单（命令子串匹配）。默认合并 shell_tool
                          Shell 禁止模式及补充规则。
        confirm_keywords: 需确认的危险关键词。默认复用 shell_tool _CONFIRM_KEYWORDS。
        workspace:        允许写操作的根目录，文件路径越界即拒绝。None=不校验路径。
    """

    # Shell 工具基础规则以外的拒绝项
    _EXTRA_DENY = ("sudo", "shutdown", "reboot")

    def __init__(
        self,
        deny_patterns: tuple[str, ...] | None = None,
        confirm_keywords: tuple[str, ...] | None = None,
        workspace: str | None = None,
        registry: ToolRegistry | None = None,
        constraints: ExecutionConstraints = ExecutionConstraints(),
        profile: str = "interactive",
        constrained_environment: bool = False,
        approval_mode: str = "ask",
    ) -> None:
        if approval_mode not in {"ask", "auto", "full"}:
            raise ValueError("unknown approval mode")
        if profile not in {"interactive", "unattended"}:
            raise ValueError("unknown permission profile")
        if profile == "unattended" and not constrained_environment:
            raise ValueError("unattended requires an explicitly constrained execution environment")
        self.profile = profile
        self.approval_mode = approval_mode
        extra = tuple(p for p in self._EXTRA_DENY if p not in _BLOCKED_PATTERNS)
        self.deny_patterns = deny_patterns if deny_patterns is not None \
            else (_BLOCKED_PATTERNS + extra)
        self.confirm_keywords = confirm_keywords if confirm_keywords is not None \
            else _CONFIRM_KEYWORDS
        self.workspace = workspace
        self.registry = registry
        self.constraints = constraints

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def check(self, block: ToolUseBlock) -> PermissionDecision:
        """
        校验一次工具调用。
        shell 工具：黑名单 DENY；可证明只读则 ALLOW；其余保守 CONFIRM。
        文件写工具：路径越界 DENY。
        MCP 工具：只有可信 server 明确只读的 adapter 才 ALLOW，其余 CONFIRM。
        其它：ALLOW。
        """
        if self.constraints.read_only and not read_only_allows(block):
            return _deny(f"read_only: tool is not in the strict read-only subset: {block.name}")
        if self.approval_mode == "full":
            return ALLOW

        # shell 命令
        if block.name in ("shell", "bash"):
            return self._check_command(block.input.get("cmd")
                                       or block.input.get("command") or "")

        # 文件工具：读取和写入都受工作区边界约束（
        # 防 LLM 读 workspace 外的系统文件如 /etc/passwd 泄露）
        if block.name in _PATH_BOUND_TOOLS:
            path = block.input.get("path", "")
            if self.workspace and path:
                reason = _path_escape_reason(path, self.workspace)
                if reason:
                    return _deny(reason)
            return ALLOW

        # MCP capability：只有 adapter 明确分类为 READ_ONLY 才默认允许。
        # remote annotations 只是 hint，因此未知/可变更 capability 一律要求确认。
        if block.name.startswith("mcp__"):
            tool = self.registry.get_tool(block.name) if self.registry is not None else None
            if tool is not None and tool.effect is ToolEffect.READ_ONLY:
                return ALLOW
            return _confirm(f"MCP mutation-capable or unknown tool: {block.name}")

        return ALLOW

    def _check_command(self, command: str) -> PermissionDecision:
        cmd_lower = command.lower()
        if self.profile == "unattended" and command.strip().split(" ", 1)[0] in {"env", "printenv"}:
            return _deny("unattended: environment inspection is not authorized")
        for pattern in self.deny_patterns:
            if pattern.lower() in cmd_lower:
                return _deny(f"'{pattern}' is on the deny list")
        if _is_repository_readonly(command):
            return ALLOW
        # Trusted interactive development may run a plain test command. The safe
        # run profile rejects shell before reaching this branch.
        if self.profile == "interactive" and re.fullmatch(
            r"(?:python(?:3)? -m )?pytest(?: [A-Za-z0-9_./:-]+)*", command.strip()
        ):
            return ALLOW
        # 仅可信入口显式启用受限环境；不因为 runtime 名为 Docker 就自动授权。
        if self.profile == "unattended" and command.strip() in {
            "python --version", "python3 --version", "pytest --version",
        }:
            return ALLOW
        if self.profile == "unattended":
            return _deny("unattended: command requires interactive authorization")
        for kw in self.confirm_keywords:
            if kw in cmd_lower:
                return _confirm(f"destructive command: {command}")
        return _confirm(f"shell command is not provably read-only: {command}")


def _path_escape_reason(path: str, workspace: str) -> str:
    """
    检查路径是否超出工作区。
    返回空串表示安全，非空为拒绝原因。
    """
    import os
    ws = os.path.realpath(workspace)
    try:
        target = os.path.realpath(os.path.join(ws, path)) if not os.path.isabs(path) \
            else os.path.realpath(path)
    except Exception as e:  # noqa: BLE001
        return f"invalid path {path!r}: {e}"
    if target == ws or target.startswith(ws + os.sep):
        return ""
    return f"path escapes workspace: {path}"
