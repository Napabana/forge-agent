import json
import subprocess
from pathlib import Path

import pytest

from agent.core import AgentConfig
from agent.event_log import EventLog
from agent.runner import ExecutionRunner, RunRequest
from agent.task import Action, ActionType, Task, ToolCall
from config.schema import DurableRuntimeConfig
from context.compaction import TraceableCompaction
from durable_runtime.store import DurableRuntimeStore
from llm.base import MockBackend
from project_memory.identity import ProjectIdentityResolver
from tools.base import ToolRegistry
from tools.file_tool import FileWriteTool


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "fixture"], ["config", "user.email", "test@example.invalid"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "original.txt").write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "original.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _interrupt(*args, **kwargs):
    raise KeyboardInterrupt


def test_isolated_resume_reuses_original_checkout_and_tool_result(tmp_path, monkeypatch, record_property):
    from agent.runner import AcceptanceContract

    repo = _repo(tmp_path)
    root = tmp_path / "runtime"
    task = Task("Write an isolated fixture", str(repo))
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("file_write", {"path": "written.txt", "content": "saved"})),
        Action(ActionType.FINISH, "", message="done"),
    ])
    registry_calls = []

    def registry_builder(config, confirm, runtime, *, default_cwd, workspace):
        registry_calls.append(workspace)
        return ToolRegistry().register(FileWriteTool(workspace))

    config = AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(root)))

    def run(resume):
        runner = ExecutionRunner(backend=backend, registry=ToolRegistry(), registry_builder=registry_builder,
                                 config=config, log_dir=str(tmp_path / "logs"))
        try:
            return runner.run(RunRequest(task, isolate=True, run_id="isolate-run", resume=resume,
                                         acceptance=AcceptanceContract(required_paths=("written.txt",))))
        finally:
            runner.close()

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_observation", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(False)

    def forbidden_write(*args):
        pytest.fail("the committed worktree mutation was repeated")

    monkeypatch.setattr(FileWriteTool, "execute", forbidden_write)
    result = run(True)
    assert result.is_success() and backend.call_count == 2
    assert result.acceptance_status == "passed"
    record_property("checkpoint_recovery", 1)
    assert registry_calls[0] == registry_calls[1] == Path(result.worktree.path)
    assert (Path(result.worktree.path) / "written.txt").read_text(encoding="utf-8") == "saved"
    assert not (repo / "written.txt").exists()
    with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo), root) as store:
        assert store._conn.execute("SELECT COUNT(*) FROM operations WHERE name='file_write'").fetchone()[0] == 1
    record_property("duplicate_side_effect_executions", 0)
    record_property("result_replay_correct", 1)


def test_isolated_run_can_pause_and_resume_from_cli(tmp_path, monkeypatch, record_property):
    from click.testing import CliRunner
    from entry.cli import cli

    repo = _repo(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "agent:\n  log_dir: " + json.dumps(str(tmp_path / "logs")) + "\n"
        "context:\n  compaction_mode: 'off'\n"
        "memory:\n  enabled: false\n"
        "durable_runtime:\n  enabled: true\n  root: " + json.dumps(str(tmp_path / "runtime")) + "\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    common = ["--config", str(config)]
    runtime = ["runtime", "--repo", str(repo)]

    def invoke(*args):
        result = runner.invoke(cli, [*common, *args])
        assert result.exit_code == 0, result.output or result.exception
        return result.output

    class PausingBackend(MockBackend):
        def complete(self, messages, tools):
            action = super().complete(messages, tools)
            if self.call_count == 2:
                run_id = json.loads(invoke(*runtime, "runs"))[0]["run_id"]
                invoke(*runtime, "pause", run_id)
            return action

    backend = PausingBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("file_write", {"path": "fixture.txt", "content": "saved"})),
        Action(ActionType.REFLECTION, "check work"),
        Action(ActionType.FINISH, "", message="resumed isolated task"),
    ])
    monkeypatch.setattr("entry.cli.create_backend_from_config", lambda options: backend)
    runner.invoke(cli, [*common, "run", "--repo", str(repo), "--isolate", "--task", "Create fixture.txt"])
    runs = json.loads(invoke(*runtime, "runs"))
    assert len(runs) == 1 and runs[0]["status"] == "paused"
    run_id = runs[0]["run_id"]
    paused = json.loads(invoke(*runtime, "inspect", run_id))
    worktree = paused["checkpoint"]["worktree"]
    assert (Path(worktree["path"]) / "fixture.txt").read_text(encoding="utf-8") == "saved"
    assert not (repo / "fixture.txt").exists()

    assert "resumed isolated task" in invoke(*runtime, "resume", run_id)
    completed = json.loads(invoke(*runtime, "inspect", run_id))
    assert completed["status"] == "completed"
    assert completed["checkpoint"]["worktree"] == worktree
    assert backend.call_count == 3
    record_property("checkpoint_recovery", 1)
    record_property("model_calls", backend.call_count)


def test_worker_resume_uses_saved_action_after_lead_and_worker_interruption(tmp_path, monkeypatch, record_property):
    repo = _repo(tmp_path)
    root = tmp_path / "runtime"
    lead = MockBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("delegate_tasks", {"tasks": [{"role": "explorer", "task": "Read fixture"}]})),
        Action(ActionType.FINISH, "", message="lead finished"),
    ])
    worker = MockBackend([Action(ActionType.FINISH, "", message=json.dumps({"summary": "saved worker"}))])
    config = AgentConfig(multi_agent_enabled=True, repo_map_mode="none",
                         durable_runtime_config=DurableRuntimeConfig(True, str(root)))
    task = Task("Review fixture", str(repo))

    def execute(resume):
        with_runner = ExecutionRunner(backend=lead, registry=ToolRegistry(), config=config,
                                      worker_backend_factory=lambda _: worker, log_dir=str(tmp_path / "logs"))
        try:
            return with_runner.run(RunRequest(task, run_id="lead-run", resume=resume))
        finally:
            with_runner.close()

    original = EventLog.log_action

    def interrupt_worker(log, step, action, *args, **kwargs):
        if action.message and "saved worker" in action.message:
            raise KeyboardInterrupt
        return original(log, step, action, *args, **kwargs)

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_action", interrupt_worker)
        with pytest.raises(KeyboardInterrupt):
            execute(False)
    result = execute(True)
    assert result.is_success()
    assert lead.call_count == 2 and worker.call_count == 1
    record_property("worker_model_calls", worker.call_count)
    record_property("checkpoint_recovery", 1)
    assert result.worker_usage.llm_calls == 1
    with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo), root) as store:
        child = store._conn.execute(
            "SELECT scope_id, owner_epoch FROM runs WHERE entrypoint='worker' AND parent_run_id='lead-run' AND status='completed'"
        ).fetchall()
        lead_row = store._conn.execute("SELECT scope_id, owner_epoch FROM runs WHERE run_id='lead-run'").fetchone()
        assert len(child) == 1 and tuple(child[0]) == tuple(lead_row) == ("lead-run", 2)


def test_resume_pending_path_acceptance_without_model_or_tool_replay(tmp_path, monkeypatch, record_property):
    from agent.runner import AcceptanceContract
    from durable_runtime import StoreFormatError

    repo = _repo(tmp_path)
    root = tmp_path / "runtime"
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("file_write", {"path": "deliverable.txt", "content": "accepted"})),
        Action(ActionType.FINISH, "", message="finished before verification"),
    ])
    task = Task("Write the fixture", str(repo))
    config = AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(root)))

    def execute(resume):
        runner = ExecutionRunner(backend=backend, registry=ToolRegistry().register(FileWriteTool(repo)),
                                 config=config, log_dir=str(repo / "acceptance-logs"))
        try:
            return runner.run(RunRequest(task, run_id="acceptance", resume=resume,
                                         acceptance=AcceptanceContract(required_paths=("deliverable.txt",))))
        finally:
            runner.close()

    with monkeypatch.context() as cut:
        cut.setattr("agent.runner._check_independent_acceptance", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            execute(False)
    deliverable = repo / "deliverable.txt"
    deliverable.write_text("outside change", encoding="utf-8")
    with pytest.raises(StoreFormatError, match="workspace changed"):
        execute(True)
    deliverable.write_text("accepted", encoding="utf-8")
    with monkeypatch.context() as cut:
        cut.setattr("agent.runner._record_post_run_trace", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            execute(True)
    deliverable.write_text("change after committed acceptance", encoding="utf-8")
    with pytest.raises(StoreFormatError, match="workspace changed"):
        execute(True)
    deliverable.write_text("accepted", encoding="utf-8")
    resumed = execute(True)
    assert resumed.is_success() and resumed.acceptance_status == "passed"
    assert backend.call_count == 2
    record_property("checkpoint_recovery", 1)
    record_property("acceptance_resume_correct", 1)


def test_active_compaction_resume_reuses_archived_view_without_recompacting(tmp_path, monkeypatch, record_property):
    from context.structured_compaction import SemanticContextFields, SemanticSummaryResult
    from llm.cache_audit import call_provider
    from llm.usage import TokenUsage

    class AuditedBackend(MockBackend):
        attempted = False

        def complete(self, messages, tools):
            def send(**_):
                if not self.attempted:
                    self.attempted = True
                    raise TimeoutError("synthetic first attempt")
                return super(AuditedBackend, self).complete(messages, tools)

            return call_provider("fixture", send, model="fixture", input=[])

    class Summarizer:
        def summarize(self, **_):
            return call_provider(
                "fixture", lambda **body: SemanticSummaryResult(
                    SemanticContextFields(), TokenUsage(input_tokens=20, cached_tokens=5), False,
                ), model="fixture", input=[],
            )

    repo = _repo(tmp_path)
    root = tmp_path / "runtime"
    backend = AuditedBackend([
        Action(ActionType.REFLECTION, "Prior details " + "example token output " * 1500),
        Action(ActionType.REFLECTION, "Recent details " + "example surviving output " * 400),
        Action(ActionType.FINISH, "", message="saved compacted result"),
    ])
    task = Task("Summarize a fixture", str(repo))

    def execute(resume):
        policy = TraceableCompaction(threshold=0.30, target_ratio=0.20, keep_recent_tokens=120,
                                     semantic_summarizer=Summarizer())
        config = AgentConfig(repo_map_mode="none", budget_tokens=3500, prepare_next_turn=policy,
                             llm_retry_delay=0,
                             durable_runtime_config=DurableRuntimeConfig(True, str(root)))
        runner = ExecutionRunner(backend=backend, registry=ToolRegistry(), config=config, log_dir=str(tmp_path / "logs"))
        try:
            return runner.run(RunRequest(task, run_id="compacted", resume=resume))
        finally:
            runner.close()

    original = EventLog.log_action

    def interrupt_second(log, step, action, *args, **kwargs):
        if step == 3:
            raise KeyboardInterrupt
        return original(log, step, action, *args, **kwargs)

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_action", interrupt_second)
        with pytest.raises(KeyboardInterrupt):
            execute(False)
    with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo), root) as store:
        row = store._conn.execute(
            "SELECT payload_json FROM run_checkpoints c JOIN runs r ON r.checkpoint_id=c.checkpoint_id"
        ).fetchone()
        saved = json.loads(row[0])
        assert saved["prepared_view_step"] == 3 and saved["model_archive_revision"] > 0

    def forbidden_compaction(*args):
        pytest.fail("the committed compaction was repeated")

    monkeypatch.setattr(TraceableCompaction, "__call__", forbidden_compaction)
    assert execute(True).summary == "saved compacted result"
    assert backend.call_count == 3
    with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo), root) as store:
        rows = store._conn.execute(
            "SELECT request_kind, attempt_index, prompt_tokens, cached_tokens, logical_request_id "
            "FROM cache_audit_events ORDER BY created_at"
        ).fetchall()
        assert tuple(rows[0])[:4] == ("generation", 1, None, None)
        assert tuple(rows[1])[:4] == ("generation", 2, 100, 0)
        assert rows[0]["logical_request_id"] == rows[1]["logical_request_id"]
        assert any(tuple(row)[:4] == ("semantic_compaction", 1, 20, 5) for row in rows)
    record_property("cache_usage_attribution_correct", 1)
    record_property("checkpoint_recovery", 1)
    record_property("duplicate_compaction_calls", 0)
