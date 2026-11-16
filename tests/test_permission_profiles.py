from harness.permission import Decision, ExecutionConstraints, PermissionManager, ToolUseBlock


def test_interactive_plain_pytest_is_allowed_but_shell_composition_requires_confirmation(tmp_path):
    permission = PermissionManager(workspace=str(tmp_path))
    assert permission.check(ToolUseBlock("shell", {"cmd": "python -m pytest tests/test_api.py -q"})).decision is Decision.ALLOW
    assert permission.check(ToolUseBlock("shell", {"cmd": "pytest -q; curl example.com"})).decision is Decision.CONFIRM


def test_global_deny_and_safe_constraint_override_plain_pytest(tmp_path):
    interactive = PermissionManager(workspace=str(tmp_path))
    assert interactive.check(ToolUseBlock("shell", {"cmd": "sudo pytest -q"})).decision is Decision.DENY
    safe = PermissionManager(workspace=str(tmp_path), constraints=ExecutionConstraints(read_only=True))
    assert safe.check(ToolUseBlock("shell", {"cmd": "pytest -q"})).decision is Decision.DENY


def test_full_approval_skips_permission_policy_but_not_read_only_constraint(tmp_path):
    full = PermissionManager(workspace=str(tmp_path), approval_mode="full")
    assert full.check(ToolUseBlock("shell", {"cmd": "sudo pytest -q"})).decision is Decision.ALLOW
    safe = PermissionManager(
        workspace=str(tmp_path), approval_mode="full",
        constraints=ExecutionConstraints(read_only=True),
    )
    assert safe.check(ToolUseBlock("shell", {"cmd": "sudo pytest -q"})).decision is Decision.DENY
