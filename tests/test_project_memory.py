from __future__ import annotations

import subprocess
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent.core import AgentConfig
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.session_store import JsonChatSessionStore
from agent.task import Action, ActionType, Task, ToolCall
from config.schema import MemoryConfig, _parse
from config.schema import AppConfig
from entry.chat import ChatSession
from agent.coordinator import AgentCoordinator, WorkerRequest
from entry.cli import _build_run_registry
from entry.context_common import build_context_policy
from entry.cli import cli
from task.engine import TaskEngine
from llm.base import MockBackend
from project_memory.identity import ProjectIdentityResolver, normalize_remote
from project_memory.runtime import MemoryRuntime
from project_memory.store import MemoryStore
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileWriteTool


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_identity_and_file_boundaries(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    for repo in (first, second):
        repo.mkdir()
        git(repo, "init")
        git(repo, "remote", "add", "origin", "https://user:secret@example.com/Team/Repo.git?token=secret")
    assert normalize_remote("git@example.com:Team/Repo.git") == "example.com/team/repo"
    a, b = ProjectIdentityResolver.resolve(first), ProjectIdentityResolver.resolve(second)
    assert a.project_id == b.project_id
    assert "secret" not in a.key
    store = MemoryStore(a, tmp_path / "store")
    with pytest.raises(ValueError, match="unsafe"):
        store.path("../escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.memory.mkdir(parents=True)
    (store.memory / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="leaves"):
        store.path("memory/link/escape")


def test_worktree_common_dir_and_non_git_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-m", "initial")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-b", "memory-test", str(worktree))
    assert ProjectIdentityResolver.resolve(repo).project_id == ProjectIdentityResolver.resolve(worktree).project_id
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ProjectIdentityResolver.resolve(plain).source == "path"


def test_run_propose_consolidate_and_reuse_on_first_request(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    config = AgentConfig(repo_map_mode="none", finish_policy="advisory", memory_config=settings,
                         approval_mode="full")
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "write code", tool_call=ToolCall("file_write", {
            "path": "app.py", "content": "answer = 1\n",
        })),
        Action(ActionType.TOOL_CALL, "save knowledge", tool_call=ToolCall("memory_propose", {
            "type": "feature", "title": "Answer module", "content": "app.py defines answer",
            "related_files": ["app.py"], "tags": ["answer"],
        })),
        Action(ActionType.FINISH, "done", message="Added answer module"),
    ])
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry().register(FileWriteTool(workspace=repo)), config=config,
                             log_dir=str(tmp_path / "logs"))
    result = runner.run(RunRequest(Task("Build answer module", str(repo)),
                                   acceptance=AcceptanceContract(verifier=lambda _: True)))
    runner.close()
    assert result.is_success() and result.acceptance_status == "passed"
    assert "app.py defines answer" not in Path(result.trace_path).read_text(encoding="utf-8")
    memory = MemoryRuntime(str(repo), root=settings.root)
    rows = memory.store.read_records()
    assert len(rows) == 1 and rows[0]["confidence"] == "verified"
    assert len(memory.store.read_activity()) == 1

    again = MockBackend([Action(ActionType.FINISH, "done", message="Checked answer")])
    runner = ExecutionRunner(backend=again, registry=ToolRegistry(), config=config,
                             log_dir=str(tmp_path / "logs"))
    runner.run(RunRequest(Task("Inspect answer module", str(repo))))
    runner.close()
    assert "Answer module" in again.received_messages[0][0].content
    assert "untrusted historical data" in again.received_messages[0][0].content
    (repo / "app.py").write_text("answer = 2\n")
    assert memory.retrieval.search("answer")[0]["stale"] is True


def test_passing_test_trace_can_verify_candidate_without_hidden_acceptance(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "write", tool_call=ToolCall("file_write", {"path": "module.py", "content": "x = 1\n"})),
        Action(ActionType.TOOL_CALL, "verify", tool_call=ToolCall("test", {})),
        Action(ActionType.TOOL_CALL, "remember", tool_call=ToolCall("memory_propose", {
            "type": "feature", "title": "Module x", "content": "module.py defines x",
            "related_files": ["module.py"], "tags": ["x"],
        })),
        Action(ActionType.FINISH, "done", message="Added x"),
    ])
    runner = ExecutionRunner(backend=backend,
        registry=ToolRegistry().register(FileWriteTool(workspace=repo)).register(NoopTool("test")),
        config=AgentConfig(repo_map_mode="none", finish_policy="advisory", approval_mode="full",
                           memory_config=settings), log_dir=str(tmp_path / "logs"))
    result = runner.run(RunRequest(Task("Add x", str(repo))))
    runner.close()
    assert result.is_success() and result.acceptance_status == "not_requested"
    rows = MemoryRuntime(str(repo), root=settings.root).store.read_records()
    assert len(rows) == 1 and rows[0]["confidence"] == "verified"


def test_bad_memory_configuration_and_pending_on_failure(tmp_path):
    with pytest.raises(ValueError, match="memory.enabled"):
        _parse({"memory": {"enabled": "yes"}})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1")
    runtime = MemoryRuntime(str(repo), root=str(tmp_path / "memories"))
    candidate = runtime.writer.propose(kind="feature", title="X", content="x exists", related_files=["a.py"],
                                       tags=["x"], run_id="failed-run", repo=str(repo))
    assert runtime.writer.pending()[0]["candidate_id"] == candidate
    assert runtime.store.read_records() == []
    again = runtime.writer.propose(kind="feature", title="X", content="x exists", related_files=["a.py"],
                                   tags=["x"], run_id="failed-run", repo=str(repo))
    assert again == candidate and len(runtime.writer.pending()) == 1
    (repo / ".env.local").write_text("API_KEY=placeholder")
    with pytest.raises(ValueError, match="unsafe"):
        runtime.writer.propose(kind="feature", title="Secret", content="look", related_files=[".env.local"],
                               tags=[], run_id="failed-run", repo=str(repo))
    (repo / "a.py").write_text("x = 2")
    with pytest.raises(ValueError, match="source files changed"):
        runtime.writer.resolve(candidate, status="approved", repo=str(repo))


def test_atomic_write_failure_preserves_previous_memory(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = MemoryRuntime(str(repo), root=str(tmp_path / "memories"))
    runtime.store.initialize()
    runtime.store.write("memory/project.md", "old")
    def fail_replace(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        runtime.store.write("memory/project.md", "new")
    assert runtime.store.path("memory/project.md").read_text() == "old"


def test_memory_cli_status_approve_search_and_reject(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("value = 1")
    root = tmp_path / "memories"
    config = tmp_path / "config.yaml"
    config.write_text(f"memory:\n  root: '{root.as_posix()}'\n")
    runtime = MemoryRuntime(str(repo), root=str(root))
    first = runtime.writer.propose(kind="feature", title="Module value", content="module.py defines value",
        related_files=["module.py"], tags=["value"], run_id="seed", repo=str(repo))
    second = runtime.writer.propose(kind="decision", title="Deferred design", content="Consider later",
        related_files=[], tags=[], run_id="seed", repo=str(repo))
    runner = CliRunner()
    prefix = ["--config", str(config), "memory"]
    status = runner.invoke(cli, prefix + ["status", "--repo", str(repo)])
    assert status.exit_code == 0 and '"pending_count": 2' in status.output
    approved = runner.invoke(cli, prefix + ["approve", "--repo", str(repo), first])
    assert approved.exit_code == 0
    searched = runner.invoke(cli, prefix + ["search", "--repo", str(repo), "value"])
    assert searched.exit_code == 0 and "Module value" in searched.output
    rejected = runner.invoke(cli, prefix + ["reject", "--repo", str(repo), second])
    assert rejected.exit_code == 0
    assert runtime.writer.pending()[0]["status"] in {"approved", "rejected"}


def test_confirmed_decision_supersedes_old_record(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = MemoryRuntime(str(repo), root=str(tmp_path / "memories"))
    old = runtime.writer.propose(kind="decision", title="Old design", content="Use A", related_files=[],
        tags=["design"], run_id="one", repo=str(repo))
    runtime.writer.resolve(old, status="approved", repo=str(repo))
    new = runtime.writer.propose(kind="decision", title="New design", content="Use B", related_files=[],
        tags=["design"], run_id="two", repo=str(repo), supersedes=[old])
    runtime.writer.resolve(new, status="approved", repo=str(repo))
    records = {row["memory_id"]: row for row in runtime.store.read_records()}
    assert records[old]["status"] == "superseded" and records[old]["superseded_by"] == new
    assert {row["memory_id"] for row in runtime.retrieval.search("design")} == {new}


def test_concurrent_activity_append_and_corrupt_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "memories"
    code = ("import sys; from project_memory.runtime import MemoryRuntime; "
            "MemoryRuntime(sys.argv[1], root=sys.argv[2]).writer.append_activity({'run_id': sys.argv[3]})")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(repo), str(root), str(i)],
                                  cwd=Path(__file__).parents[1]) for i in range(4)]
    assert all(process.wait(timeout=15) == 0 for process in processes)
    runtime = MemoryRuntime(str(repo), root=str(root))
    assert {row["run_id"] for row in runtime.store.read_activity()} == {str(i) for i in range(4)}
    pending = runtime.store.path("memory/pending/bad.json")
    pending.parent.mkdir(parents=True)
    pending.write_text("{broken", encoding="utf-8")
    assert runtime.writer.pending() == []


def test_new_chat_and_read_only_worker_share_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "feature.py").write_text("flag = True\n")
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    runtime = MemoryRuntime(str(repo), root=settings.root)
    candidate = runtime.writer.propose(kind="feature", title="Feature flag", content="feature.py defines flag",
        related_files=["feature.py"], tags=["flag"], run_id="seed", repo=str(repo))
    runtime.writer.resolve(candidate, status="approved", repo=str(repo))

    cfg = AppConfig(memory=settings)
    cfg.agent.log_dir = str(tmp_path / "logs")
    chat_backend = MockBackend([Action(ActionType.FINISH, "done", message="Reviewed flag"),
                                Action(ActionType.FINISH, "done", message="Reviewed again")])
    chat = ChatSession(chat_backend, ToolRegistry(), cfg, str(repo), cfg.agent.log_dir,
                       stream=False, reasoning_stream=False)
    chat.run_round("Inspect feature flag")
    assert "Feature flag" in chat_backend.received_messages[0][0].content
    chat.run_round("Inspect flag again")
    assert chat.agent is chat.runner.agent
    chat.runner.close()

    worker_backend = MockBackend([Action(ActionType.FINISH, "done", message='{"summary":"checked","memory_candidates":[{"title":"candidate"}]}')])
    seen_tools = []
    original_complete = worker_backend.complete
    def capture_tools(messages, tools):
        seen_tools.extend(tool.name for tool in tools)
        return original_complete(messages, tools)
    worker_backend.complete = capture_tools
    coordinator = AgentCoordinator(repo_path=str(repo), log_dir=str(tmp_path / "worker_logs"),
        parent_task_id="lead", backend_factory=lambda role: worker_backend,
        registry_factory=_build_run_registry,
        agent_config=AgentConfig(repo_map_mode="none", memory_config=settings),
        engine=TaskEngine(tmp_path / "tasks.db"))
    before = len(runtime.store.read_activity())
    result = coordinator.delegate([WorkerRequest("explorer", "Inspect feature flag")])[0]
    assert "Feature flag" in worker_backend.received_messages[0][0].content
    assert result.memory_candidates == [{"title": "candidate"}]
    assert "memory_search" in seen_tools and "memory_read" in seen_tools
    assert "memory_propose" not in seen_tools
    assert len(runtime.store.read_activity()) == before


def test_isolate_reads_shared_identity_but_does_not_promote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "feature.py").write_text("flag = True\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-m", "initial")
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    runtime = MemoryRuntime(str(repo), root=settings.root)
    candidate = runtime.writer.propose(kind="feature", title="Feature flag", content="feature.py defines flag",
        related_files=["feature.py"], tags=["flag"], run_id="seed", repo=str(repo))
    runtime.writer.resolve(candidate, status="approved", repo=str(repo))
    backend = MockBackend([Action(ActionType.FINISH, "done", message="Inspected flag")])
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry(),
        config=AgentConfig(repo_map_mode="none", finish_policy="advisory", memory_config=settings),
        log_dir=str(tmp_path / "logs"), registry_builder=_build_run_registry)
    result = runner.run(RunRequest(Task("Inspect feature flag", str(repo)), isolate=True,
                                   result_policy="discard"))
    runner.close()
    assert result.is_success()
    assert "Feature flag" in backend.received_messages[0][0].content
    assert runtime.store.read_activity()[-1]["isolate"] is True
    assert len(runtime.store.read_records()) == 1


def test_resumed_chat_preflight_includes_memory_on_first_request(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "feature.py").write_text("flag = True\n")
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    runtime = MemoryRuntime(str(repo), root=settings.root)
    candidate = runtime.writer.propose(kind="feature", title="Flag behavior", content="feature.py defines flag",
        related_files=["feature.py"], tags=["flag"], run_id="seed", repo=str(repo))
    runtime.writer.resolve(candidate, status="approved", repo=str(repo))
    cfg = AppConfig(memory=settings)
    cfg.agent.log_dir = str(tmp_path / "logs")
    store = JsonChatSessionStore(tmp_path / "sessions")
    first_backend = MockBackend([Action(ActionType.FINISH, "done", message="first")])
    first = ChatSession(first_backend, ToolRegistry(), cfg, str(repo), cfg.agent.log_dir,
        stream=False, reasoning_stream=False, session_store=store,
        prepare_next_turn=build_context_policy(cfg, first_backend))
    first.run_round("Inspect flag")
    session_id = first.session_id
    first.runner.close()
    second_backend = MockBackend([Action(ActionType.FINISH, "done", message="second")])
    resumed = ChatSession(second_backend, ToolRegistry(), cfg, str(repo), cfg.agent.log_dir,
        stream=False, reasoning_stream=False, session_store=store, session_id=session_id,
        prepare_next_turn=build_context_policy(cfg, second_backend))
    resumed.run_round("Review flag again")
    resumed.runner.close()
    assert "Flag behavior" in second_backend.received_messages[0][0].content


def test_evaluation_memory_disabled_by_default(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = MockBackend([Action(ActionType.FINISH, "done", message="done")])
    settings = MemoryConfig(root=str(tmp_path / "memories"))
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry(),
        config=AgentConfig(repo_map_mode="none", finish_policy="advisory", memory_config=settings),
        log_dir=str(tmp_path / "logs"))
    runner.run(RunRequest(Task("Inspect", str(repo)), entrypoint="eval"))
    runner.close()
    assert "memory_search" not in backend.received_messages[0][0].content
    assert not (tmp_path / "memories").exists()
