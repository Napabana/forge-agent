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
    HookBlockResult,
    HookEvent,
    Hooks,
    PermissionManager,
    ToolExecutor,
    ToolUseBlock,
)
from tools.base import BaseTool, ToolErrorType, ToolRegistry, ToolResult
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
    r.register(EchoTool("file_read"))
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

    def test_python_inline_code_is_not_treated_as_readonly(self):
        perm = PermissionManager()
        dec = perm.check(
            block("shell", cmd="python -c \"open('x','w').write('y')\"")
        )
        assert dec.is_confirm

    def test_unknown_shell_command_fails_safe_to_confirm(self):
        perm = PermissionManager()
        dec = perm.check(block("shell", cmd="custom-tool --do-something"))
        assert dec.is_confirm

    def test_byte_inspection_tools_are_readonly(self):
        perm = PermissionManager()
        assert perm.check(block("shell", cmd="xxd config.py")).is_allow
        assert perm.check(block("shell", cmd="od -c config.py")).is_allow
        assert perm.check(block("shell", cmd="strings artifact.bin")).is_allow

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

    def test_file_read_path_escape_denied(self, tmp_path):
        """M4：读工具也受 workspace 边界约束（防读系统文件泄露）。"""
        perm = PermissionManager(workspace=str(tmp_path))
        dec = perm.check(block("file_read", path="/etc/passwd"))
        assert dec.is_deny
        assert "escapes" in dec.reason or "workspace" in dec.reason

    def test_file_view_in_workspace_allowed(self, tmp_path):
        perm = PermissionManager(workspace=str(tmp_path))
        assert perm.check(block("file_view", path="notes.txt")).is_allow

    def test_workspace_none_skips_path_check(self):
        """workspace=None 时所有文件工具都放行（向后兼容）。"""
        perm = PermissionManager()
        assert perm.check(block("file_read", path="/etc/passwd")).is_allow
        assert perm.check(block("file_write", path="/etc/x")).is_allow

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

    def test_invalid_arguments_stop_before_hooks_and_keep_error_type(self, registry):
        seen = []
        hooks = Hooks().register(HookEvent.PRE_TOOL_USE, lambda block: seen.append(block))

        result = ToolExecutor(registry, hooks=hooks).execute("echo", {"text": 123})

        assert not result.success
        assert result.error_type is ToolErrorType.INVALID_ARGUMENTS
        assert result.to_observation("echo").error_type == "invalid_arguments"
        assert seen == []

    def test_permission_denies_blocked_command(self, registry):
        # 即便底层是 echo 工具，permission 看 block.name=="shell" 也校验；
        # 这里用 shell 名 + 危险参数验证拒绝路径
        perm = PermissionManager()
        ex = ToolExecutor(registry, permission=perm)
        result = ex.execute("shell", {"cmd": "rm -rf /"})
        assert not result.success
        assert result.error_type is ToolErrorType.PERMISSION_DENIED
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

    def test_decision_callback_reports_allow_and_deny(self, registry, tmp_path):
        """M4：decision_callback 在 check 之后触发，allow/deny 都上报。"""
        perm = PermissionManager(workspace=str(tmp_path))
        seen = []
        ex = ToolExecutor(
            registry, permission=perm,
            decision_callback=lambda name, params, dec: seen.append((name, dec.decision.value)),
        )
        ex.execute("file_read", {"path": "/etc/passwd"})   # deny
        ex.execute("file_read", {"path": str(tmp_path / "x")})  # allow（文件不存在不影响决策）
        names = {n for n, _ in seen}
        assert ("file_read", "deny") in seen
        assert ("file_read", "allow") in seen

    def test_no_decision_callback_is_noop(self, registry, tmp_path):
        """默认无 decision_callback：行为不变（零回归）。"""
        perm = PermissionManager(workspace=str(tmp_path))
        ex = ToolExecutor(registry, permission=perm)
        result = ex.execute("file_read", {"path": "/etc/passwd"})
        assert not result.success   # 仍被 DENY

    def test_pre_tool_hook_short_circuits(self, registry):
        hooks = Hooks()
        hooks.register(HookEvent.PRE_TOOL_USE, lambda b: "vetoed by hook")
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "x"})
        assert not result.success
        assert result.error_type is ToolErrorType.HOOK_BLOCKED
        assert "vetoed" in result.error

    def test_typed_pre_hook_block_and_exception_fail_closed(self, registry):
        blocked = Hooks().register(
            HookEvent.PRE_TOOL_USE,
            lambda _block: HookBlockResult("typed veto"),
        )
        failed = Hooks().register(
            HookEvent.PRE_TOOL_USE,
            lambda _block: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        blocked_result = ToolExecutor(registry, hooks=blocked).execute("echo", {"text": "x"})
        failed_result = ToolExecutor(registry, hooks=failed).execute("echo", {"text": "x"})

        assert blocked_result.error_type is ToolErrorType.HOOK_BLOCKED
        assert failed_result.error_type is ToolErrorType.HOOK_FAILED

    def test_post_tool_hook_observed(self, registry):
        seen = []
        hooks = Hooks()

        def observe(block, result):
            seen.append((block.name, result.output))
            return ToolResult(False, "replacement")

        hooks.register(HookEvent.POST_TOOL_USE, observe)
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "y"})
        assert result.success
        assert result.output == "y"
        assert seen == [("echo", "y")]

    def test_post_tool_hook_error_does_not_break_flow(self, registry):
        hooks = Hooks()
        hooks.register(HookEvent.POST_TOOL_USE, lambda b, r: (_ for _ in ()).throw(RuntimeError("boom")))
        ex = ToolExecutor(registry, hooks=hooks)
        result = ex.execute("echo", {"text": "z"})
        assert result.success   # 观察钩子抛错不影响主流程
        assert result.output == "z"
        assert result.diagnostics == ("post_tool_hook:RuntimeError",)

    def test_unknown_tool(self, registry):
        ex = ToolExecutor(registry)
        result = ex.execute("nope", {})
        assert not result.success
        assert result.error_type is ToolErrorType.UNKNOWN_TOOL
        assert "Unknown" in (result.error or "")

    def test_tool_failures_are_classified(self):
        def fake_tool(name, result=None, error=None):
            tool = EchoTool(name)
            tool.execute = lambda _params: result if error is None else (_ for _ in ()).throw(error)
            return tool

        registry = ToolRegistry()
        registry.register(fake_tool("timeout", ToolResult(False, "", "command timed out")))
        registry.register(fake_tool("shell", ToolResult(False, "", "Docker is not available")))
        registry.register(fake_tool("boom", error=RuntimeError("boom")))

        assert ToolExecutor(registry).execute("timeout", {}).error_type is ToolErrorType.TIMEOUT
        assert ToolExecutor(registry).execute("shell", {}).error_type is ToolErrorType.INFRASTRUCTURE
        assert ToolExecutor(registry).execute("boom", {}).error_type is ToolErrorType.TOOL_EXECUTION
