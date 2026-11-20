import subprocess
import sys

import pytest

from agent.task import Action, ActionType, Task, ToolCall
from durable_runtime import DurableRuntimeStore, StoreFormatError
from durable_runtime.run_manager import DurableRun
from durable_runtime.operation_journal import OutcomeUnknown, ToolOperation
from durable_runtime.recovery import resolve_operation
from llm.base import LLMMessage, LLMResponse
from llm.usage import TokenUsage
from project_memory.identity import ProjectIdentity
from tools.base import ToolResult


def test_model_action_and_native_call_pair_survive_reopen(tmp_path):
    identity = ProjectIdentity("test", "path", str(tmp_path))
    history = [{"role": "user", "content": "Read the fixture"}]
    action = Action(ActionType.TOOL_CALL, "", ToolCall("file_read", {"path": "fixture"}))
    native = {"call_id": "call_1", "name": "file_read", "arguments": '{"path":"fixture"}'}
    response = LLMResponse(action, "", TokenUsage(input_tokens=20, output_tokens=8), responses_call=native)
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        run.create(Task("Read the fixture", str(tmp_path)), "cli")
        call = run.start_model(1, [LLMMessage(**history[0])], [], {"history": history})
        run.commit_model(call, 1, response, {"history": history})
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        checkpoint = run.load_checkpoint()
        restored = run.pending_response(checkpoint)
        assert checkpoint["history"] == history
        assert restored.action == action
        assert restored.responses_call == native
        assert restored.usage == response.usage
        run.start_model(2, [], [], {"history": history})
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        checkpoint = run.load_checkpoint()
        assert checkpoint["next_step"] == 2
        assert run.pending_response(checkpoint) is None
        assert store._conn.execute("SELECT count(*) FROM model_calls WHERE status='outcome_unknown' AND usage_json IS NULL").fetchone()[0] == 1


def test_checkpoint_tampering_is_detected(tmp_path):
    identity = ProjectIdentity("test", "path", str(tmp_path))
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        run.create(Task("task", str(tmp_path)), "cli")
        store._conn.execute("UPDATE run_checkpoints SET payload_json='{}'")
        with pytest.raises(StoreFormatError, match="digest"):
            run.load_checkpoint()


def test_committed_result_replays_and_unknown_effect_blocks_mutation(tmp_path):
    identity = ProjectIdentity("test", "path", str(tmp_path))
    options = {"read_only": False, "decision": None}
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        run.create(Task("task", str(tmp_path)), "cli")
        completed = ToolOperation(run, "complete", 1, None)
        assert completed.prepare("write", {"path": "one"}, **options) is None
        completed.start()
        completed.finish("write", ToolResult(True, "written"))
        unknown = ToolOperation(run, "interrupted", 2, None)
        assert unknown.prepare("write", {"path": "two"}, **options) is None
        unknown.start()
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        completed = ToolOperation(run, "complete", 1, None)
        assert completed.prepare("write", {"path": "one"}, **options) == ToolResult(True, "written")
        with pytest.raises(OutcomeUnknown):
            ToolOperation(run, "interrupted", 2, None).prepare("write", {"path": "two"}, **options)
        with pytest.raises(OutcomeUnknown):
            ToolOperation(run, "new", 3, None).prepare("write", {"path": "three"}, **options)
        assert ToolOperation(run, "inspect", 3, None).prepare("read", {}, read_only=True, decision=None) is None


def test_runner_persists_agent_and_acceptance_results(tmp_path):
    from agent.core import AgentConfig
    from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
    from config.schema import DurableRuntimeConfig
    from llm.base import MockBackend
    from project_memory.identity import ProjectIdentityResolver
    from tools.base import ToolRegistry

    repo = tmp_path / "repo"
    repo.mkdir()
    runtime_root = tmp_path / "runtime"
    runner = ExecutionRunner(
        backend=MockBackend([Action(ActionType.FINISH, "", message="finished")]),
        registry=ToolRegistry(), log_dir=str(tmp_path / "logs"),
        config=AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(runtime_root))),
    )
    result = runner.run(RunRequest(Task("Summarize", str(repo)), acceptance=AcceptanceContract(verifier=lambda _: False)))
    assert result.run_id
    with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo), runtime_root) as store:
        run = DurableRun(store, result.run_id, result.run_id)
        checkpoint = run.load_checkpoint()
        assert checkpoint["agent_result"]["status"] == "success"
        assert checkpoint["result"]["acceptance_status"] == "failed"
        assert "pending_model_call" not in checkpoint


def test_crashed_process_requires_explicit_resolution(tmp_path):
    identity = ProjectIdentity("test", "path", str(tmp_path))
    process = subprocess.run([
        sys.executable, "-c", """
import os
import sys
from agent.task import Task
from durable_runtime import DurableRuntimeStore
from durable_runtime.operation_journal import ToolOperation
from durable_runtime.run_manager import DurableRun
from project_memory.identity import ProjectIdentity
root = sys.argv[1]
store = DurableRuntimeStore(ProjectIdentity("test", "path", root), root)
run = DurableRun(store, "run", "scope")
run.create(Task("task", root), "cli")
run.start_model(1, [], [], {})
operation = ToolOperation(run, "call", 1, None)
operation.prepare("write", {}, read_only=False, decision=None)
operation.start()
os._exit(42)
""", str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert process.returncode == 42, process.stderr
    resolution = {"action": "resolve_not_applied", "actor": "test",
                  "reason": "checked the workspace", "evidence": {"file_absent": True}}
    with DurableRuntimeStore(identity, tmp_path) as store:
        op_id = store._conn.execute("SELECT operation_id FROM operations").fetchone()[0]
        resolve_operation(store, op_id, "decision", **resolution)
        assert store._conn.execute("SELECT state FROM operations").fetchone()[0] == "resolved_not_applied"
        assert store._conn.execute("SELECT status FROM runs").fetchone()[0] == "interrupted"
    with DurableRuntimeStore(identity, tmp_path) as store:
        resolve_operation(store, op_id, "decision", **resolution)
        assert store._conn.execute("SELECT count(*) FROM recovery_decisions").fetchone()[0] == 1
    with DurableRuntimeStore(identity, tmp_path) as store:
        with pytest.raises(StoreFormatError, match="different evidence"):
            resolve_operation(store, op_id, "decision", **{**resolution, "evidence": {"different": True}})


def test_cli_runtime_verify_and_backup(tmp_path):
    import json

    from click.testing import CliRunner
    from entry.cli import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(f"durable_runtime:\n  root: {json.dumps(str(tmp_path / 'runtime'))}\n", encoding="utf-8")
    args = ["--config", str(config), "runtime", "--repo", str(repo)]
    runner = CliRunner()
    result = runner.invoke(cli, [*args, "verify"])
    assert result.exit_code == 0, result.exception
    assert result.output.strip() == "ok"
    result = runner.invoke(cli, [*args, "recover", "list"])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.output) == []
    backup = tmp_path / "snapshot.db"
    result = runner.invoke(cli, [*args, "backup", str(backup)])
    assert result.exit_code == 0, result.exception
    assert backup.is_file()


def test_direct_resume_replays_saved_finish_without_calling_model(tmp_path, monkeypatch):
    from agent.core import AgentConfig
    from agent.event_log import EventLog
    from agent.runner import ExecutionRunner, RunRequest
    from config.schema import DurableRuntimeConfig
    from llm.base import MockBackend
    from tools.base import ToolRegistry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fixture.txt").write_text("fixture", encoding="utf-8")
    task = Task("Summarize", str(repo))
    backend = MockBackend([Action(ActionType.FINISH, "", message="saved answer")])
    config = AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(tmp_path / "runtime")))
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry(), config=config, log_dir=str(tmp_path / "logs"))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_action", interrupt)
        with pytest.raises(KeyboardInterrupt):
            runner.run(RunRequest(task, run_id="resume-me"))

    def forbidden_model_call(*args, **kwargs):
        pytest.fail("resumed Run made another model request")

    monkeypatch.setattr(backend, "complete", forbidden_model_call)
    result = runner.run(RunRequest(task, run_id="resume-me", resume=True))
    assert result.is_success()
    assert result.summary == "saved answer"
    assert result.steps_taken == 1


def test_cli_resume_reuses_saved_action(tmp_path, monkeypatch):
    import json

    from agent.event_log import EventLog
    from click.testing import CliRunner
    from entry.cli import cli
    from llm.base import MockBackend

    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        "agent:\n  log_dir: " + json.dumps(str(tmp_path / "logs")) + "\n"
        "context:\n  compaction_mode: 'off'\n"
        "memory:\n  enabled: false\n"
        "durable_runtime:\n  enabled: true\n  root: " + json.dumps(str(tmp_path / "runtime")) + "\n",
        encoding="utf-8",
    )
    backend = MockBackend([Action(ActionType.FINISH, "", message="saved CLI answer")])
    monkeypatch.setattr("entry.cli.create_backend_from_config", lambda options: backend)
    cli_runner = CliRunner()
    args = ["--config", str(config)]

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_action", interrupt)
        stopped = cli_runner.invoke(cli, [*args, "run", "--repo", str(repo), "--task", "Summarize"])
        assert stopped.exit_code != 0
    runtime = [*args, "runtime", "--repo", str(repo)]
    runs = cli_runner.invoke(cli, [*runtime, "runs"])
    assert runs.exit_code == 0, runs.exception
    run_id = json.loads(runs.output)[0]["run_id"]
    resumed = cli_runner.invoke(cli, [*runtime, "resume", run_id])
    assert resumed.exit_code == 0, resumed.exception
    assert "saved CLI answer" in resumed.output
    runs = cli_runner.invoke(cli, [*runtime, "runs"])
    assert len(json.loads(runs.output)) == 1


def test_lost_file_tool_result_can_be_inspected_without_repeating_write(tmp_path, monkeypatch):
    from durable_runtime.reconcilers import inspect_file_operation
    from harness.executor import ToolExecutor
    from project_memory.identity import ProjectIdentityResolver
    from tools.base import ToolRegistry
    from tools.file_tool import FileWriteTool

    repo = tmp_path / "repo"
    repo.mkdir()
    identity = ProjectIdentityResolver.resolve(repo)
    runtime = tmp_path / "runtime"
    with DurableRuntimeStore(identity, runtime) as store:
        run = DurableRun(store, "run", "run")
        run.create(Task("write the fixture", str(repo)), "cli")
        executor = ToolExecutor(ToolRegistry().register(FileWriteTool(repo)))
        operation = ToolOperation(run, "call", 1, None)

        def lost_result(*args):
            raise KeyboardInterrupt

        monkeypatch.setattr(operation, "finish", lost_result)
        with pytest.raises(KeyboardInterrupt):
            executor.execute("file_write", {"path": "fixture.txt", "content": "saved\n"}, operation=operation)
    with DurableRuntimeStore(identity, runtime) as store:
        run = DurableRun(store, "run", "run")
        row = store._conn.execute("SELECT * FROM operations").fetchone()
        assert row["state"] == "outcome_unknown"
        assert inspect_file_operation(row, identity)["outcome"] == "applied"
        (repo / "fixture.txt").write_text("operator edit", encoding="utf-8")
        assert inspect_file_operation(row, identity)["outcome"] == "unknown"


def test_direct_resume_reuses_tool_result_and_refuses_changed_workspace(tmp_path, monkeypatch):
    from agent.core import AgentConfig
    from agent.event_log import EventLog
    from agent.runner import ExecutionRunner, RunRequest
    from config.schema import DurableRuntimeConfig
    from llm.base import MockBackend
    from tools.base import ToolRegistry
    from tools.file_tool import FileWriteTool

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = repo / "fixture.txt"
    target.write_text("original\n", encoding="utf-8")
    task = Task("Write the fixture", str(repo))
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("file_write", {"path": "fixture.txt", "content": "written\n"})),
        Action(ActionType.FINISH, "", message="done"),
    ])
    tool = FileWriteTool(repo)
    config = AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(tmp_path / "runtime")))
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry().register(tool), config=config,
                             log_dir=str(tmp_path / "logs"))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as cut:
        cut.setattr(EventLog, "log_observation", interrupt)
        with pytest.raises(KeyboardInterrupt):
            runner.run(RunRequest(task, run_id="resume-me"))
    assert target.read_text(encoding="utf-8") == "written\n"

    def forbid_tool(*args, **kwargs):
        pytest.fail("committed tool was re-executed")

    monkeypatch.setattr(tool, "execute", forbid_tool)
    target.write_text("changed\n", encoding="utf-8")
    with pytest.raises(StoreFormatError, match="workspace changed"):
        runner.run(RunRequest(task, run_id="resume-me", resume=True))
    target.write_text("written\n", encoding="utf-8")
    assert runner.run(RunRequest(task, run_id="resume-me", resume=True)).is_success()


def test_verified_file_retry_links_new_operation_to_original(tmp_path):
    from durable_runtime.recovery import authorize_file_retry
    from harness.executor import ToolExecutor
    from project_memory.identity import ProjectIdentityResolver
    from tools.base import ToolRegistry
    from tools.file_tool import FileWriteTool

    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "runtime"
    identity = ProjectIdentityResolver.resolve(repo)
    executor = ToolExecutor(ToolRegistry().register(FileWriteTool(repo)))
    params = {"path": "fixture.txt", "content": "confirmed"}

    def interrupt():
        raise KeyboardInterrupt

    with DurableRuntimeStore(identity, root) as store:
        run = DurableRun(store, "run", "scope")
        run.create(Task("write the fixture", str(repo)), "cli")
        original = ToolOperation(run, "call", 1, None)
        with pytest.raises(KeyboardInterrupt):
            executor.execute("file_write", params, invocation_callback=interrupt, operation=original)
    with DurableRuntimeStore(identity, root) as store:
        resolve_operation(store, original.operation_id, "not-applied", action="resolve_not_applied",
                          actor="fixture", reason="checked", evidence={"file_absent": True})
    with DurableRuntimeStore(identity, root) as store:
        authorize_file_retry(store, original.operation_id, "retry", actor="fixture", reason="verified before state")
    with DurableRuntimeStore(identity, root) as store:
        run = DurableRun(store, "run", "scope")
        retry = ToolOperation(run, "call", 1, None)
        assert retry.operation_id != original.operation_id
        result = executor.execute("file_write", params, operation=retry)
        assert result.success
        rows = store._conn.execute("SELECT state, parent_operation_id FROM operations ORDER BY created_at").fetchall()
        assert [tuple(row) for row in rows] == [("resolved_not_applied", None),
                                                 ("result_committed", original.operation_id)]
        assert (repo / "fixture.txt").read_text(encoding="utf-8") == "confirmed"


def test_pause_is_acknowledged_between_steps_and_resumes(tmp_path):
    from agent.core import AgentConfig
    from agent.runner import ExecutionRunner, RunRequest
    from config.schema import DurableRuntimeConfig
    from llm.base import MockBackend
    from project_memory.identity import ProjectIdentityResolver
    from tools.base import ToolRegistry

    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "runtime"
    identity = ProjectIdentityResolver.resolve(repo)

    class PausingModel(MockBackend):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if self.call_count == 1:
                with DurableRuntimeStore(identity, root) as store:
                    store.request_pause("pausable")
            return response

    backend = PausingModel([Action(ActionType.REFLECTION, "keep working"),
                            Action(ActionType.FINISH, "", message="done")])
    config = AgentConfig(repo_map_mode="none", durable_runtime_config=DurableRuntimeConfig(True, str(root)))
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry(), config=config, log_dir=str(tmp_path / "logs"))
    task = Task("Summarize", str(repo))
    paused = runner.run(RunRequest(task, run_id="pausable"))
    assert paused.termination_reason == "paused"
    assert backend.call_count == 1
    with DurableRuntimeStore(identity, root) as store:
        assert store._conn.execute("SELECT status FROM runs").fetchone()[0] == "paused"
    finished = runner.run(RunRequest(task, run_id="pausable", resume=True))
    assert finished.is_success() and finished.steps_taken == 2
