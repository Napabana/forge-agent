import json
import dataclasses
import sqlite3
import subprocess
import threading
import time
import pytest

from agent.coordinator import AgentCoordinator, DelegateTasksTool, PROFILES, WorkerRequest
from agent.core import AgentConfig
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from entry.cli import _build_run_registry
from entry.context_common import new_worker_backend
from config.schema import load_config
from llm.base import MockBackend
from task.engine import TaskEngine
from tools.base import ToolRegistry
from tools.base import ToolResult


class SlowBackend(MockBackend):
    def __init__(self, tracker, role, delay=0.1):
        super().__init__([Action(ActionType.FINISH, "done", message=json.dumps({
            "summary": role, "findings": [{"file": "sample.py", "line": 1, "evidence": "sample"}],
            "recommendations": ["inspect"]}))])
        self.tracker = tracker
        self.delay = delay

    def complete(self, messages, tools):
        with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        try:
            time.sleep(self.delay)
            return super().complete(messages, tools)
        finally:
            with self.tracker["lock"]:
                self.tracker["active"] -= 1


def _tracker():
    return {"lock": threading.Lock(), "active": 0, "peak": 0}


def _coordinator(tmp_path, backend_factory, **kwargs):
    return AgentCoordinator(
        repo_path=str(tmp_path), log_dir=str(tmp_path / "logs"), parent_task_id="lead-1",
        backend_factory=backend_factory, registry_factory=_build_run_registry,
        agent_config=AgentConfig(repo_map_mode="none"),
        engine=TaskEngine(tmp_path / "tasks.db"), **kwargs,
    )


def test_parallel_workers_have_independent_trace_task_and_usage(tmp_path):
    tracker = _tracker()
    created = []

    def backend_factory(role):
        backend = SlowBackend(tracker, role)
        created.append(backend)
        return backend

    coordinator = _coordinator(tmp_path, backend_factory)
    results = coordinator.delegate([WorkerRequest("explorer", "Find code"),
                                    WorkerRequest("reviewer", "Review diff")])
    assert tracker["peak"] >= 2
    assert len({id(backend) for backend in created}) == 2
    assert all(len(backend.received_messages) == 1 for backend in created)
    assert all(backend.model_name == "mock-model" for backend in created)
    assert all(result.status == "success" for result in results)
    assert len({result.trace_reference for result in results}) == 2
    assert all(result.parent_task_id == "lead-1" and result.findings for result in results)
    for result in results:
        first = json.loads(open(result.trace_reference, encoding="utf-8").readline())
        assert first["payload"]["task"]["parent_task_id"] == "lead-1"
    assert coordinator.usage.total_tokens == 300
    assert all(coordinator.engine.get_task(result.task_id).parent_id == "lead-1" for result in results)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        coordinator.remaining_tokens = 0
        coordinator.delegate([WorkerRequest("explorer", "Again")])


def test_delegate_role_schema_matches_executor_and_failed_role_can_be_corrected(tmp_path):
    from llm.tool_schema import strictify_tool_parameters

    coordinator = _coordinator(tmp_path, lambda role: SlowBackend(_tracker(), role, delay=0))
    delegate = DelegateTasksTool(coordinator)
    schema = strictify_tool_parameters(delegate.parameters_schema)
    assert schema["properties"]["tasks"]["items"]["properties"]["role"]["enum"] == list(PROFILES)
    invalid = delegate.execute({"tasks": [{"role": "investigator", "task": "Find code"}]})
    assert not invalid.success and all(role in invalid.error for role in PROFILES)
    assert coordinator.delegation_count == 0
    accepted = delegate.execute({"tasks": [{"role": "explorer", "task": "Find code"}]})
    result = json.loads(accepted.output)[0]
    assert accepted.success and result["findings"] and result["trace_reference"]


def test_default_worker_budget_allows_two_24k_workers(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path, lambda role: SlowBackend(_tracker(), role, delay=0.01))
    allocated = []
    run_worker = coordinator._run_worker

    def capture_budget(task_id, agent_id, request, token_limit, cancel):
        allocated.append(token_limit)
        return run_worker(task_id, agent_id, request, token_limit, cancel)

    monkeypatch.setattr(coordinator, "_run_worker", capture_budget)
    results = coordinator.delegate([
        WorkerRequest("explorer", "Find code"),
        WorkerRequest("test_analyst", "Find tests"),
    ])
    assert all(result.status == "success" for result in results)
    assert allocated == [24_000, 24_000]


def test_worker_can_finish_read_only_analysis_when_lead_requires_plan(tmp_path):
    backend = SlowBackend(_tracker(), "explorer", delay=0)
    coordinator = _coordinator(tmp_path, lambda role: backend)
    coordinator.agent_config = dataclasses.replace(coordinator.agent_config, planning_mode="always")

    result = coordinator.delegate([WorkerRequest("explorer", "Inspect the code")])[0]

    assert result.status == "success"
    assert result.findings
    assert "[Structured Planning]" not in backend.received_messages[0][0].content


def test_worker_registry_cannot_write_shared_workspace(tmp_path):
    target = tmp_path / "forbidden.py"

    def backend_factory(role):
        return MockBackend([
            Action(ActionType.TOOL_CALL, "write", tool_call=ToolCall("file_write", {
                "path": str(target), "content": "bad",
            })),
            Action(ActionType.FINISH, "done", message="done"),
        ])

    coordinator = _coordinator(tmp_path, backend_factory)
    result = coordinator.delegate([WorkerRequest("explorer", "Try write")])[0]
    assert result.status == "success"
    assert not target.exists()


def test_parent_cancel_propagates_before_provider_returns(tmp_path):
    tracker = _tracker()
    parent_cancel = threading.Event()
    coordinator = _coordinator(tmp_path, lambda role: SlowBackend(tracker, role, delay=0.3),
                               cancel_event=parent_cancel, timeout_seconds=5)
    threading.Timer(0.02, parent_cancel.set).start()
    started = time.monotonic()
    result = coordinator.delegate([WorkerRequest("explorer", "Find code")])[0]
    assert result.status == "canceled"
    assert time.monotonic() - started < 0.3


def test_task_engine_migrates_old_schema_for_child_metadata(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, subject TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', owner TEXT, worktree TEXT, created_at REAL NOT NULL)")
    engine = TaskEngine(path)
    child = engine.create_task("child", parent_id="lead", agent_id="worker-a", role="reviewer")
    record = engine.get_task(child)
    assert (record.parent_id, record.agent_id, record.role) == ("lead", "worker-a", "reviewer")


def test_sandboxed_worker_uses_readonly_docker_runtime(tmp_path, monkeypatch):
    seen = []

    class FakeDocker:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def preflight(self, **kwargs):
            return ToolResult(True, "ready")

        def cleanup(self):
            seen.append("cleanup")

    monkeypatch.setattr("tools.runtime.DockerRuntime", FakeDocker)
    tracker = _tracker()
    coordinator = _coordinator(tmp_path, lambda role: SlowBackend(tracker, role, delay=0.01),
                               sandbox_workers=True)
    result = coordinator.delegate([WorkerRequest("explorer", "Inspect")])[0]
    assert result.status == "success"
    assert seen[0]["readonly_root"] is True and seen[0]["network"] is False
    assert "cleanup" in seen


def test_role_model_override_has_its_own_context_budget(tmp_path, monkeypatch):
    config_path = tmp_path / "worker-model.yaml"
    config_path.write_text(
        "llm:\n  provider: openai\n  model: gpt-4o\n  context_window: 128000\n"
        "agent:\n  context_budget_cap: 16000\n  worker_models:\n    reviewer: custom-review-model\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    monkeypatch.setattr("entry.context_common.create_backend_from_config", lambda options: options)
    reviewer, reviewer_budget = new_worker_backend(cfg, "reviewer")
    explorer, explorer_budget = new_worker_backend(cfg, "explorer")
    assert reviewer["model"] == "custom-review-model"
    assert explorer["model"] == "gpt-4o"
    assert reviewer_budget.model_name == "custom-review-model"
    assert explorer_budget.model_name == "gpt-4o"


def test_worker_failure_returns_partial_sibling_result(tmp_path):
    tracker = _tracker()

    def backend_factory(role):
        if role == "reviewer":
            raise RuntimeError("provider unavailable")
        return SlowBackend(tracker, role, delay=0.01)

    coordinator = _coordinator(tmp_path, backend_factory)
    results = coordinator.delegate([WorkerRequest("explorer", "Find code"),
                                    WorkerRequest("reviewer", "Review diff")])
    assert {result.status for result in results} == {"success", "failed"}
    assert coordinator.usage.total_tokens == 150


def test_timeout_closes_coordinator_and_returns_partial_status(tmp_path):
    tracker = _tracker()
    coordinator = _coordinator(tmp_path, lambda role: SlowBackend(tracker, role, delay=0.2),
                               timeout_seconds=0.02)
    results = coordinator.delegate([WorkerRequest("explorer", "Find code")])
    assert results[0].status == "timed_out"
    assert coordinator.closed


def test_lead_can_delegate_then_finish_with_independent_acceptance(tmp_path):
    tracker = _tracker()
    lead = MockBackend([
        Action(ActionType.TOOL_CALL, "delegate", tool_call=ToolCall("delegate_tasks", {
            "tasks": [{"role": "explorer", "task": "Find code"},
                      {"role": "test_analyst", "task": "Find tests"}],
        })),
        Action(ActionType.FINISH, "done", message="done"),
        Action(ActionType.FINISH, "second", message="second"),
    ])
    runner = ExecutionRunner(
        backend=lead, registry=ToolRegistry(),
        config=AgentConfig(multi_agent_enabled=True, repo_map_mode="none"),
        log_dir=str(tmp_path / "logs"), registry_builder=_build_run_registry,
        worker_backend_factory=lambda role: SlowBackend(tracker, role, delay=0.01),
    )
    result = runner.run(RunRequest(Task("Inspect", str(tmp_path), max_steps=2),
                                   acceptance=AcceptanceContract(verifier=lambda path: False)))
    assert result.status is RunStatus.SUCCESS
    assert result.acceptance_status == "failed"
    assert result.worker_usage.total_tokens == 300
    assert result.total_tokens == result.usage.total_tokens == 600
    assert runner._owned_worker_engine.get_task(result.task_id).status == "completed"
    assert len([item for item in runner._owned_worker_engine.list_tasks()
                if item.parent_id == result.task_id]) == 2
    assert any('"workers_summary"' in line for line in open(result.trace_path, encoding="utf-8"))
    from evals.coding_agent.graders import extract_metrics
    metrics = extract_metrics(result, wall_time_seconds=1.0)
    assert metrics.worker_count == 2
    assert metrics.worker_failure_count == 0
    assert metrics.tool_call_count == 1 + metrics.worker_tool_call_count
    runner.config = dataclasses.replace(runner.config, multi_agent_enabled=False)
    second = runner.run(RunRequest(Task("Inspect again", str(tmp_path), max_steps=1)))
    assert second.status is RunStatus.SUCCESS
    assert "delegate_tasks" not in runner.registry.tool_names


def test_isolated_runner_delegates_inside_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "sample.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    tracker = _tracker()
    lead = MockBackend([
        Action(ActionType.TOOL_CALL, "delegate", tool_call=ToolCall("delegate_tasks", {
            "tasks": [{"role": "explorer", "task": "Inspect sample.py"}],
        })),
        Action(ActionType.FINISH, "done", message="done"),
    ])
    engine = TaskEngine(tmp_path / "tasks.db")
    runner = ExecutionRunner(
        backend=lead, registry=ToolRegistry(),
        config=AgentConfig(multi_agent_enabled=True, repo_map_mode="none"),
        log_dir=str(tmp_path / "logs"), registry_builder=_build_run_registry,
        worker_backend_factory=lambda role: SlowBackend(tracker, role, delay=0.01),
        engine=engine,
    )
    result = runner.run(RunRequest(Task("Inspect", str(repo), max_steps=2),
                                   isolate=True, result_policy="discard"))
    assert result.status is RunStatus.SUCCESS
    assert result.worker_usage.total_tokens == 150
    children = [task for task in engine.list_tasks() if task.parent_id == result.task_id]
    assert len(children) == 1 and children[0].role == "explorer"
    assert (repo / "sample.py").read_text(encoding="utf-8") == "x = 1\n"
