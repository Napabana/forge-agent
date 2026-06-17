"""
tests/test_harness.py

M2 Task 2.2：Hooks + Permission + ToolExecutor 测试。
覆盖：Hooks 注册/触发/短路；Permission 的 DENY/CONFIRM/ALLOW；
ToolExecutor 包装——黑名单拦截、CONFIRM 经 callback 放行/拒绝、透明直通、PostToolUse 触发。
"""

from __future__ import annotations

import pytest

from harness import (
    ALLOW,
    HookEvent,
    Hooks,
    PermissionManager,
    ToolExecutor,
    ToolUseBlock,
)
from tools.base import BaseTool, ToolRegistry, ToolResult
from tools.shell_tool import always_allow, always_deny


# ---------------------------------------------------------------------------
# 辅助：一个最简单的 echo 工具，用来被 ToolExecutor 包装
# ---------------------------------------------------------------------------

class EchoTool(BaseTool):
    def __init__(self, name: str = "echo"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo input"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    def to_llm_schema(self):  # 测试不需要 LLM schema
        return None

    def execute(self, params):
        return ToolResult(success=True, output=params.get("text", ""), error=None)


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(EchoTool("echo"))
    r.register(EchoTool("shell"))   # 名为 shell 的 echo，供 permission 路径测试
    return r


def block(name="shell", **params) -> ToolUseBlock:
    return ToolUseBlock(name, params)


# ===========================================================================
# Hooks
# ===========================================================================

class TestHooks:
    def test_register_and_trigger(self):
        hooks = Hooks()
        calls = []
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: calls.append(b.name) or None)
        result = hooks.trigger(HookEvent.PRE_TOOL_USE, block("shell", cmd="ls"))
        assert calls == ["shell"]
        assert result is None   # 放行

    def test_short_circuit_on_first_non_none(self):
        hooks = Hooks()
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: None)       # 放行
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: "blocked")  # 拦截
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: "should-not-run")
        result = hooks.trigger(HookEvent.PRE_TOOL_USE, block("shell"))
        assert result == "blocked"
        assert hooks.count(HookEvent.PRE_TOOL_USE) == 3

    def test_unknown_event_raises(self):
        hooks = Hooks()
        with pytest.raises(ValueError):
            hooks.register("Nope", lambda *a: None)

    def test_unregister(self):
        hooks = Hooks()
        cb = lambda b: None  # noqa: E731
        hooks.register(HookEvent.STOP, cb)
        assert hooks.unregister(HookEvent.STOP, cb) is True
        assert hooks.count(HookEvent.STOP) == 0


# ===========================================================================
# Permission
# ===========================================================================

class TestPermission:
    def test_deny_list_blocks(self):
        perm = PermissionManager()
        dec = perm.check(block("shell", cmd="rm -rf /"))
        assert dec.is_deny
        assert "rm -rf /" in dec.reason or "deny list" in dec.reason

    def test_destructive_needs_confirm(self):
        perm = PermissionManager()
        dec = perm.check(block("shell", cmd="git commit -m x"))
        assert dec.is_confirm

    def test_readonly_allowed(self):
        perm = PermissionManager()
        dec = perm.check(block("shell", cmd="ls -la"))
        assert dec.is_allow

    def test_sudo_denied(self):
        # s20 DENY_LIST 补充项 sudo
        perm = PermissionManager()
        dec = perm.check(block("shell", cmd="sudo apt install x"))
        assert dec.is_deny

    def test_file_write_path_escape_denied(self, tmp_path):
        ws = str(tmp_path)
        perm = PermissionManager(workspace=ws)
        dec = perm.check(block("file_write", path="/etc/passwd"))
        assert dec.is_deny
        assert "escapes" in dec.reason or "workspace" in dec.reason

    def test_file_write_in_workspace_allowed(self, tmp_path):
        ws = str(tmp_path)
        perm = PermissionManager(workspace=ws)
        dec = perm.check(block("file_write", path="src/app.py"))
        assert dec.is_allow

    def test_mcp_deploy_needs_confirm(self):
        perm = PermissionManager()
        dec = perm.check(block("mcp__deploy__trigger", service="prod"))
        assert dec.is_confirm


# ===========================================================================
# ToolExecutor
# ===========================================================================

class TestToolExecutor:
    def test_passthrough_without_permission_hooks(self, registry):
        # 无 permission/hooks：透明直通，等价于 registry.execute_tool
        ex = ToolExecutor(registry)
        result = ex.execute("echo", {"text": "hello"})
        assert result.success and result.output == "hello"

    def test_permission_denies_blocked_command(self, registry):
        # 即便底层是 echo 工具，permission 看 block.name=="shell" 也校验；
        # 这里用 shell 名 + 危险参数验证拒绝路径
        perm = PermissionManager()
        ex = ToolExecutor(registry, permission=perm)
        result = ex.execute("shell", {"cmd": "rm -rf /"})
        assert not result.success
        assert "deny" in result.error.lower() or "permission" in result.error.lower()

    def test_confirm_allowed_by_callback(self, registry):
        perm = PermissionManager()
        ex = ToolExecutor(registry, permission=perm, confirm_callback=always_allow)
        # "git commit" 命中 _CONFIRM_KEYWORDS → CONFIRM → callback 放行
        # （底层 echo 工具不真正执行命令，仅验证放行链路）
        result = ex.execute("shell", {"cmd": "git commit -m x"})
        assert result.success   # 放行后执行了底层 echo

    def test_confirm_rejected_by_callback(self, registry):
        perm = PermissionManager()
        ex = ToolExecutor(registry, permission=perm, confirm_callback=always_deny)
        result = ex.execute("shell", {"cmd": "git commit -m x"})
        assert not result.success
        assert "denied by user" in result.error

    def test_confirm_without_callback_denies(self, registry):
        # 无 confirm_callback：CONFIRM 一律拒绝（安全优先）
        perm = PermissionManager()
        ex = ToolExecutor(registry, permission=perm)
        result = ex.execute("shell", {"cmd": "git commit -m x"})
        assert not result.success

    def test_pre_tool_hook_short_circuits(self, registry):
        hooks = Hooks()
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: "vetoed by hook")
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "x"})
        assert not result.success
        assert "vetoed" in result.error

    def test_post_tool_hook_observed(self, registry):
        seen = []
        hooks = Hooks()
        hooks.register(HookEvent.POST_TOOL_USE, lambda b, r: seen.append((b.name, r.output)) or None)
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "y"})
        assert result.success
        assert seen == [("echo", "y")]

    def test_post_tool_hook_error_does_not_break_flow(self, registry):
        hooks = Hooks()
        hooks.register(HookEvent.POST_TOOL_USE, lambda b, r: (_ for _ in ()).throw(RuntimeError("boom")))
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "z"})
        assert result.success   # 观察钩子抛错不影响主流程
        assert result.output == "z"

    def test_unknown_tool(self, registry):
        ex = ToolExecutor(registry)
        result = ex.execute("nope", {})
        assert not result.success
        assert "Unknown" in (result.error or "")
