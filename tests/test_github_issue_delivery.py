from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.event_log import EventLog
from agent.task import EventType, RunResult, RunStatus, Task
from entry import github_issue


_BRANCH = "agent/fix-issue-1"


def _git(repo, *args):
    """在临时仓库执行测试所需的最小 Git 命令。"""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path, with_remote=True):
    """创建隔离的本地仓库，并按需连接本地 bare remote。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Forge Test")
    _git(repo, "config", "user.email", "forge@example.invalid")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", _BRANCH)
    if with_remote:
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        _git(repo, "remote", "add", "origin", str(remote))
    return repo


def _result(acceptance_status="passed"):
    """构造已完成 Agent 与可控独立验收状态。"""
    return RunResult(
        "delivery",
        RunStatus.SUCCESS,
        "done",
        1,
        acceptance_status=acceptance_status,
    )


def _deliver(result, repo, pr_creator):
    """使用固定交付参数调用被测入口。"""
    return github_issue.deliver_pull_request(
        result,
        str(repo),
        _BRANCH,
        "fix: resolve issue #1",
        "owner/repo",
        "Fix issue",
        "body",
        pr_creator=pr_creator,
    )


def test_clone_uses_ephemeral_auth_and_tokenless_remote(tmp_path, monkeypatch):
    """clone 参数和持久化 remote 不得包含 Token，认证只进入子进程环境。"""
    token = "github_pat_fake_secret"
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        target = Path(command[-1])
        (target / ".git").mkdir(parents=True)
        (target / ".git" / "config").write_text(f"url = {command[-2]}\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    monkeypatch.setattr(github_issue.subprocess, "run", fake_run)
    target = tmp_path / "clone"
    github_issue.clone_repo("owner/repo", str(target))

    command_text = " ".join(captured["command"])
    remote = (target / ".git" / "config").read_text()
    assert command_text == f"git clone https://github.com/owner/repo.git {target}"
    assert token not in command_text and token not in remote
    assert token not in captured["env"].values()
    assert (
        captured["env"]["GIT_CONFIG_COUNT"] == "1"
        and "GIT_TRACE_CURL" not in captured["env"]
    )


def test_clone_failure_does_not_expose_token(tmp_path, monkeypatch):
    """clone 失败信息只包含无凭据 URL，不泄露临时认证内容。"""
    token = "github_pat_fake_secret"

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            128,
            "",
            f"fatal: cannot clone {command[-2]}",
        )

    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr(github_issue.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc_info:
        github_issue.clone_repo("owner/repo", str(tmp_path / "clone"))
    assert token not in str(exc_info.value)


@pytest.mark.parametrize("create_pr", [True, False])
def test_issue_registry_uses_target_repo(tmp_path, monkeypatch, create_pr):
    """自动 PR 独占提交权，no-pr 则保留原有提交工具。"""
    import agent.runner as runner_module
    from config.schema import AppConfig
    from entry import cli
    from llm import router
    from tools.base import NoopTool, ToolRegistry

    class RegistryReached(Exception):
        """在完成 registry 参数断言后终止入口，避免启动真实 Agent。"""

    target = str(tmp_path)
    monkeypatch.setattr("config.schema.load_config", lambda _path: AppConfig())
    monkeypatch.setattr(
        github_issue,
        "fetch_issue",
        lambda *_args: ("title", "add tests", "url"),
    )
    monkeypatch.setattr(github_issue, "clone_repo", lambda *_args: None)
    monkeypatch.setattr(
        github_issue,
        "_run_git",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(github_issue, "create_branch", lambda *_args: None)
    monkeypatch.setattr(router, "create_backend_from_config", lambda _config: object())
    registry = (
        ToolRegistry()
        .register(NoopTool("git_add"))
        .register(NoopTool("git_commit"))
    )

    def assert_registry(_config, **kwargs):
        assert kwargs == {"default_cwd": target, "workspace": target}
        return registry

    class AssertRunner:
        def __init__(self, **kwargs):
            expected = [] if create_pr else ["git_add", "git_commit"]
            assert kwargs["registry"].tool_names == expected

        def run(self, _request, **kwargs):
            assert callable(kwargs["on_event"])
            raise RegistryReached

    monkeypatch.setattr(cli, "_build_registry", assert_registry)
    monkeypatch.setattr(runner_module, "ExecutionRunner", AssertRunner)
    with pytest.raises(RegistryReached):
        github_issue.run_on_issue(
            "owner/repo",
            4,
            target,
            create_pr=create_pr,
            verify_command="python -V" if create_pr else None,
        )


def test_delivery_stops_when_no_diff(tmp_path):
    repo = _repo(tmp_path)
    calls = []
    result = _result()

    assert _deliver(result, repo, lambda **kwargs: calls.append(kwargs)) is None
    assert result.delivery_status == "no_changes" and calls == []


def test_delivery_stops_before_commit_when_acceptance_failed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "change.txt").write_text("change\n")
    result = _result("failed")

    assert _deliver(result, repo, lambda **_kwargs: "unused") is None
    assert result.delivery_status == "blocked_acceptance"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert "change.txt" in _git(repo, "status", "--porcelain")


def test_push_failure_retains_local_commit(tmp_path):
    repo = _repo(tmp_path, with_remote=False)
    (repo / "change.txt").write_text("change\n")
    result = _result()

    assert _deliver(result, repo, lambda **_kwargs: "unused") is None
    assert result.delivery_status == "push_failed"
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo, "status", "--porcelain") == ""


def test_pr_retry_does_not_repeat_commit_or_push(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "change.txt").write_text("change\n")
    result = _result()
    push_calls, pr_calls = [], []
    original_run_git = github_issue._run_git

    def recording_run_git(args, cwd, env=None):
        if args and args[0] == "push":
            push_calls.append(tuple(args))
        return original_run_git(args, cwd, env)

    def flaky_pr_creator(**kwargs):
        pr_calls.append(kwargs)
        if len(pr_calls) == 1:
            raise RuntimeError("temporary PR failure")
        return "https://example.invalid/pr/1"

    monkeypatch.setattr(github_issue, "_run_git", recording_run_git)
    assert _deliver(result, repo, flaky_pr_creator) is None
    assert result.delivery_status == "pr_failed"
    assert _deliver(result, repo, flaky_pr_creator) == "https://example.invalid/pr/1"
    assert result.delivery_status == "delivered"
    assert len(push_calls) == 1 and len(pr_calls) == 2
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo, "rev-parse", "HEAD") == _git(
        repo,
        "rev-parse",
        f"refs/remotes/origin/{_BRANCH}",
    )


def test_delivery_outcome_appends_to_existing_trace(tmp_path):
    task = Task("issue", str(tmp_path), task_id="delivery-trace")
    log = EventLog.create(
        task,
        log_dir=str(tmp_path / "logs"),
        entrypoint="github_issue",
    )
    log.log_task_start(task)
    trace_path = str(log.path)
    log.close()

    result = RunResult(
        task.task_id,
        RunStatus.SUCCESS,
        "done",
        2,
        trace_path=trace_path,
        acceptance_status="passed",
        delivery_status="delivered",
        termination_reason="completion_satisfied",
    )
    observed = []
    github_issue._record_delivery_trace(
        result,
        requested=True,
        repo_name="owner/repo",
        branch=_BRANCH,
        issue_number=1,
        pr_url="https://example.invalid/pr/1",
        on_event=observed.append,
    )

    read_log = EventLog.open_existing(trace_path)
    try:
        events = read_log.replay()
    finally:
        read_log.close()
    delivery = next(
        event for event in events if event.event_type is EventType.DELIVERY
    )
    assert delivery.payload["entrypoint"] == "github_issue"
    assert delivery.payload["run_id"] == events[0].payload["run_id"]
    assert delivery.payload["run_span_id"] == events[0].payload["run_span_id"]
    assert delivery.payload["status"] == "delivered"
    assert delivery.payload["pr_url"] == "https://example.invalid/pr/1"
    assert observed[-1].event_type is EventType.DELIVERY
