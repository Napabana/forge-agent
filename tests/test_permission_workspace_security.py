from harness.permission import PermissionManager, ToolUseBlock
import pytest


def test_read_tool_cannot_follow_workspace_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    manager = PermissionManager(workspace=str(workspace))
    assert manager.check(ToolUseBlock("file_read", {"path": "escape/secret"})).is_deny


def test_safe_file_within_workspace_remains_allowed(tmp_path):
    manager = PermissionManager(workspace=str(tmp_path))
    assert manager.check(ToolUseBlock("file_read", {"path": "src/main.py"})).is_allow


def test_unattended_requires_trusted_environment_assertion():
    with pytest.raises(ValueError):
        PermissionManager(profile="unattended")


@pytest.mark.parametrize("command", ["python3 --version", "pytest --version", "git status"])
def test_unattended_allows_only_known_diagnostics(command):
    manager = PermissionManager(profile="unattended", constrained_environment=True)
    assert manager.check(ToolUseBlock("shell", {"cmd": command})).is_allow


@pytest.mark.parametrize("command", ["rm -rf src", "python -c 'import os'", "env", "pwd && curl example.com", "python3 --version; rm src"])
def test_unattended_keeps_unknown_combinations_and_credentials_denied(command):
    manager = PermissionManager(profile="unattended", constrained_environment=True)
    assert manager.check(ToolUseBlock("shell", {"cmd": command})).is_deny
