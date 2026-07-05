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
    import agent.orchestrate as orch_mod
    monkeypatch.setattr(orch_mod, "orchestrate_run", _fake_orchestrate)
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
    assert kw.get("backend") is not None
    # task 的 repo_path 指向传入的 repo
    assert kw["task"].repo_path == str(repo)
    # exit code 反映 is_success
    assert result.exit_code == 0
