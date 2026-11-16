"""
tests/test_cli_isolate.py

M4 第二波：CLI `agent run --isolate` 标志测试。

验证：
- --isolate 出现在 run --help
- --isolate 时走 orchestrate_run（而非内联 agent.run），且隔离路径不调真 LLM
  （monkeypatch orchestrate_run 返回固定 RunResult）
- 默认（无 --isolate）走原同步路径，行为不变
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner


def test_isolate_in_run_help():
    from entry.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert "--isolate" in result.output
    assert "--result-policy" in result.output


def test_isolate_invokes_orchestrate_run(tmp_path, monkeypatch):
    """--isolate 时调用 orchestrate_run（不烧 API）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# x")
    # 造个 git 仓库（orchestrate_run 需要 worktree，worktree 需要至少一次提交）
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    called = {}

    class _FakeResult:
        def is_success(self): return True
        @property
        def status(self):
            class S:
                value = "success"
            return S()
        steps_taken = 1
        total_tokens = 0
        error = None

    async def _fake_orchestrate(**kwargs):
        called["kwargs"] = kwargs
        return _FakeResult()

    # CLI run 里是函数内 `from agent.orchestrate import orchestrate_run`，
    # 所以 patch 源头模块的属性，让那次 import 拿到假函数。
    import agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "orchestrate_run", _fake_orchestrate)
    monkeypatch.setattr(runner_mod, "TaskEngine", lambda _path: object())
    # 同时 patch backend 工厂，避免真 LLM 配置/调用
    import entry.cli as cli_mod
    monkeypatch.setattr(cli_mod, "create_backend_from_config",
                        lambda cfg: object(), raising=False)
    # cli run 里 `from llm.router import create_backend_from_config` 也是函数内 import，
    # 所以再 patch 源头
    import llm.router as router_mod
    monkeypatch.setattr(router_mod, "create_backend_from_config",
                        lambda cfg: object())

    from entry.cli import cli
    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--task", "do something",
              "--repo", str(repo), "--isolate"],
        catch_exceptions=False,
    )

    # orchestrate_run 被调用
    assert "kwargs" in called, "--isolate 应触发 orchestrate_run"
    kw = called["kwargs"]
    assert kw.get("sandbox") is False
    assert kw.get("result_policy") == "keep-if-changed"
    assert kw.get("backend") is not None
    assert callable(kw.get("on_event"))
    # task 的 repo_path 指向传入的 repo
    assert kw["task"].repo_path == str(repo)
    # exit code 反映 is_success
    assert result.exit_code == 0


def test_isolate_accepts_discard_result_policy(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    called = {}

    class _FakeResult:
        def is_success(self): return True
        @property
        def status(self):
            class S:
                value = "success"
            return S()
        steps_taken = 0
        total_tokens = 0
        error = None
        worktree = None

    async def _fake_orchestrate(**kwargs):
        called.update(kwargs)
        return _FakeResult()

    import agent.runner as runner_mod
    import entry.cli as cli_mod
    import llm.router as router_mod
    monkeypatch.setattr(runner_mod, "orchestrate_run", _fake_orchestrate)
    monkeypatch.setattr(runner_mod, "TaskEngine", lambda _path: object())
    monkeypatch.setattr(cli_mod, "create_backend_from_config", lambda cfg: object())
    monkeypatch.setattr(router_mod, "create_backend_from_config", lambda cfg: object())

    from entry.cli import cli
    result = CliRunner().invoke(
        cli,
        [
            "run", "--task", "verify only", "--repo", str(repo),
            "--isolate", "--result-policy", "discard",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called["result_policy"] == "discard"


def test_build_registry_injects_workspace_into_file_tools(tmp_path):
    """普通 run/chat registry 应把 repo workspace 下发给文件工具。"""
    from config.schema import AppConfig
    from entry.cli import _build_registry

    registry = _build_registry(AppConfig(), workspace=str(tmp_path))

    denied = registry.execute_tool("file_read", {"path": "/etc/passwd"})
    assert not denied.success
    assert "escapes workspace" in denied.error.lower()

    ok = registry.execute_tool("file_write", {"path": "inside.txt", "content": "ok"})
    assert ok.success
    assert (tmp_path / "inside.txt").read_text() == "ok"

    edited = registry.execute_tool(
        "file_edit",
        {"path": "inside.txt", "old_text": "ok", "new_text": "edited"},
    )
    assert edited.success
    assert (tmp_path / "inside.txt").read_text() == "edited"


def test_build_registry_keeps_process_cwd_independent_from_file_workspace(tmp_path):
    """process tools 绑定 target repo cwd，不再借用 worktree_path 或 workspace。"""
    from config.schema import AppConfig
    from entry.cli import _build_registry

    cwd = tmp_path / "repo"
    workspace = tmp_path / "files"
    cwd.mkdir()
    workspace.mkdir()
    registry = _build_registry(
        AppConfig(), default_cwd=str(cwd), workspace=str(workspace),
    )

    for name in ("shell", "test", "git_status", "git_diff", "git_add", "git_commit"):
        assert registry._tools[name]._default_cwd == str(cwd)
    for name in (
        "file_read", "file_view", "file_edit", "file_write",
        "search_text", "find_files", "find_symbol",
    ):
        assert registry._tools[name]._workspace == workspace.resolve()


def test_build_registry_search_tools_default_to_workspace_and_reject_escape(
    tmp_path, monkeypatch,
):
    """Search defaults must stay inside target repo instead of process cwd."""
    from config.schema import AppConfig
    from entry.cli import _build_registry

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inside.py").write_text(
        "def policy_mode():\n    return 'inside-marker'\n",
        encoding="utf-8",
    )
    (outside / "leak.py").write_text(
        "def leaked_symbol():\n    return 'outside-marker'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(outside)

    registry = _build_registry(AppConfig(), workspace=str(workspace))

    text_result = registry.execute_tool("search_text", {"pattern": "inside-marker"})
    assert text_result.success
    assert "inside.py" in text_result.output
    assert "outside-marker" not in text_result.output

    files_result = registry.execute_tool("find_files", {"pattern": "*.py"})
    assert files_result.success
    assert "inside.py" in files_result.output
    assert "leak.py" not in files_result.output

    symbol_result = registry.execute_tool("find_symbol", {"symbol": "policy_mode"})
    assert symbol_result.success
    assert "inside.py" in symbol_result.output

    for name, params in (
        ("search_text", {"pattern": "outside-marker", "path": str(outside)}),
        ("find_files", {"pattern": "*.py", "path": "../outside"}),
        ("find_symbol", {"symbol": "leaked_symbol", "path": str(outside)}),
    ):
        denied = registry.execute_tool(name, params)
        assert not denied.success
        assert denied.error_type.value == "invalid_arguments"
        assert "escapes workspace" in denied.error.lower()


def test_run_registry_hides_git_mutation_tools(tmp_path):
    from config.schema import AppConfig
    from entry.cli import _build_run_registry

    registry = _build_run_registry(
        AppConfig(),
        default_cwd=str(tmp_path),
        workspace=str(tmp_path),
    )

    assert "git_status" in registry.tool_names
    assert "git_diff" in registry.tool_names
    assert "git_add" not in registry.tool_names
    assert "git_commit" not in registry.tool_names

    blocked = registry.execute_tool("shell", {"cmd": "git commit -m should-not-run"})
    assert blocked.success is False
    assert blocked.error_type.value == "permission_denied"
    assert "disabled for this entrypoint" in blocked.error
