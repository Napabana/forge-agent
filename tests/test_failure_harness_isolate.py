"""Isolated-run failure regressions that require orchestrator composition."""

from __future__ import annotations

import shutil
from pathlib import Path

from agent.event_log import EventLog
from agent.runner import ExecutionRunner, RunRequest
from agent.task import Action, ActionType, EventType, RunStatus, Task
from llm.base import MockBackend
from runtime.worktree import (
    WorktreeArtifact,
    WorktreeChanges,
    WorktreeDisposition,
)
from task.engine import TaskEngine
from tools.base import ToolRegistry
from tools.runtime import LocalRuntime, RunResult as RuntimeRunResult


def test_isolate_sandbox_preflight_failure_is_offline_infrastructure_failure(
    tmp_path, monkeypatch
):
    """Docker absence is injected without touching Docker, network, or a provider."""
    import agent.orchestrate as orchestrate_module

    runtime_instances = []
    agent_factory_calls = []

    class FakeWorktreeSession:
        def __init__(self, repo_path, name, task_engine=None, task_id=None, **_kwargs):
            self.repo_path = Path(repo_path)
            self.name = name
            self.task_engine = task_engine
            self.task_id = task_id
            self.path = self.repo_path / ".worktrees" / name
            self.branch = f"wt/{name}"
            self.base_commit = "base"
            self.created = False

        async def create(self):
            self.path.mkdir(parents=True, exist_ok=True)
            self.created = True
            if self.task_engine is not None and self.task_id is not None:
                self.task_engine.bind_worktree(self.task_id, self.name)
            return self

        async def inspect_changes(self):
            return WorktreeChanges()

        async def finalize(self, _action, *, changes=None, partial=False):
            changes = changes or WorktreeChanges()
            shutil.rmtree(self.path, ignore_errors=True)
            return WorktreeArtifact(
                disposition=WorktreeDisposition.REMOVED,
                branch=self.branch,
                path=None,
                base_commit=self.base_commit,
                head_commit=self.base_commit,
                changed_files=changes.changed_files,
                uncommitted_count=changes.uncommitted_count,
                commit_count=changes.commit_count,
                partial=partial,
            )

    class FakeDockerRuntime(LocalRuntime):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.cleaned = False
            runtime_instances.append(self)

        @property
        def name(self):
            return "fake-docker"

        def preflight(self, cwd=None):
            assert cwd is not None
            return RuntimeRunResult(1, "Docker is not available", "")

        def cleanup(self):
            self.cleaned = True

    monkeypatch.setattr(orchestrate_module, "WorktreeSession", FakeWorktreeSession)
    monkeypatch.setattr(orchestrate_module, "DockerRuntime", FakeDockerRuntime)

    repo = tmp_path / "repo"
    repo.mkdir()
    engine = TaskEngine(tmp_path / "tasks.db")

    def registry_builder(*_args, **_kwargs):
        raise AssertionError("registry construction must not happen after failed preflight")

    runner = ExecutionRunner(
        backend=MockBackend([Action(ActionType.FINISH, "unused", message="unused")]),
        registry=ToolRegistry(),
        registry_builder=registry_builder,
        engine=engine,
        log_dir=str(tmp_path / "logs"),
    )
    result = runner.run(
        RunRequest(
            Task("sandbox preflight failure", str(repo), max_steps=1),
            isolate=True,
            sandbox=True,
            entrypoint="cli",
        )
    )

    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "infrastructure_error"
    assert result.steps_taken == 0
    assert runtime_instances and runtime_instances[0].cleaned is True
    assert agent_factory_calls == []
    assert result.trace_path

    with EventLog.open_existing(result.trace_path) as log:
        events = log.replay()
    terminated = next(
        event.payload for event in events if event.event_type is EventType.RUN_TERMINATED
    )
    assert terminated["status"] == "failed"
    assert terminated["termination_reason"] == "infrastructure_error"
    assert terminated["entrypoint"] == "cli"
