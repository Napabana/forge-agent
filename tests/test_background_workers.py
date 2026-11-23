import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent.core import AgentConfig
from agent.event_log import EventLog
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import Action, ActionType, EventType, Task, ToolCall
from config.schema import DurableRuntimeConfig
from llm.base import MockBackend
from tools.base import NoopTool, ToolEffect, ToolRegistry


def _call(name, **params):
    return Action(ActionType.TOOL_CALL, "", ToolCall(name, params))


def _delegate():
    return _call("delegate_tasks", tasks=[{"role": "reviewer", "task": "Review the sample."}])


def _finish(message="done"):
    return Action(ActionType.FINISH, "", message=message)


def _trace(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _run_comparison(tmp_path, mode, record_property):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.txt").write_text("fixture\n")
    background = mode == "background"
    single = mode == "single"
    worker_started, lead_work_started, notification = (threading.Event() for _ in range(3))
    wait_call = _call("worker_wait", task_id="assigned after dispatch")
    status_call = _call("worker_status")
    result_text = "Independent review evidence."
    observed = {}

    class Worker(MockBackend):
        def complete(self, *args):
            worker_started.set()
            if background:
                assert lead_work_started.wait(10), "Lead never ran while the Worker was active"
            time.sleep(0.10)
            return super().complete(*args)

    class LeadProbe(NoopTool):
        effect = ToolEffect.READ_ONLY

        def execute(self, params):
            assert worker_started.wait(10)
            lead_work_started.set()
            time.sleep(0.18)
            if background:
                assert notification.wait(10), "Worker never pushed a completion notification"
            return super().execute(params)

    class LocalReview(NoopTool):
        effect = ToolEffect.READ_ONLY

        def execute(self, params):
            worker_started.set()
            time.sleep(0.10)
            return super().execute(params)

    def observe(event):
        if event.event_type == EventType.WORKER_FINISHED:
            notification.set()
        if event.event_type != EventType.OBSERVATION:
            return
        observation = event.payload["observation"]
        name = observation["tool_name"]
        if name in {"delegate_tasks", "worker_status", "worker_wait"}:
            observed[name] = json.loads(observation["output"])
        if name == "delegate_tasks":
            wait_call.tool_call.params["task_id"] = observed[name][0]["task_id"]

    script = [_call("local_review") if single else _delegate()]
    if background:
        script.append(status_call)
    script.append(_call("lead_probe"))
    if background:
        script.append(wait_call)
    script.append(_finish())
    class Lead(MockBackend):
        def complete(self, messages, tools):
            if self.call_count == len(script) - (2 if background else 1):
                assert any(result_text in item.content for item in messages)
            return super().complete(messages, tools)

    lead = Lead(script)
    worker = Worker([_finish(json.dumps({"summary": result_text, "findings": [{"line": 1}]}))])
    runner = ExecutionRunner(
        backend=lead, registry=ToolRegistry().register(LeadProbe("lead_probe", "Checked sample")).register(
            LocalReview("local_review", result_text)),
        config=AgentConfig(multi_agent_enabled=not single, worker_mode="sync" if single else mode,
                           max_total_tokens=60_000, repo_map_mode="none"),
        log_dir=str(tmp_path / "logs"), worker_backend_factory=lambda _: worker,
    )
    started = time.monotonic()
    try:
        result = runner.run(RunRequest(Task("Review a fixture.", str(repo)), acceptance=AcceptanceContract(
            verifier=lambda path: (Path(path) / "sample.txt").read_text() == "fixture\n")), on_event=observe)
    finally:
        runner.close()
    elapsed = time.monotonic() - started
    assert result.is_success() and result.acceptance_status == "passed"
    record_property("wall_ms", round(elapsed * 1000))
    record_property("total_tokens", result.total_tokens)
    record_property("worker_tokens", result.worker_usage.total_tokens)
    if single:
        assert lead.call_count == 3 and worker.call_count == 0
        assert all(e["event_type"] != "worker_dispatched" for e in _trace(result.trace_path))
        record_property("lead_worker_overlap_ms", 0)
        record_property("worker_dispatch_ms", 0)
        record_property("worker_wait_ms", 0)
        record_property("worker_result_delivered", 0)
        record_property("worker_result_read", 0)
        return
    assert result.worker_usage.total_tokens == 150
    events = _trace(result.trace_path)
    receipt = observed["delegate_tasks"][0]
    worker_result = observed["worker_wait"]["result"] if background else receipt
    assert worker_result["summary"] == result_text
    assert worker_result["findings"] == [{"line": 1}]
    child = _trace(worker_result["trace_reference"])

    def event_index(kind, tool=None):
        return next(i for i, e in enumerate(events) if e["event_type"] == kind and
                    (tool is None or e["payload"].get("tool_name") == tool))

    def timestamp(event):
        return datetime.fromisoformat(event["timestamp"]).timestamp()

    def duration(kind, tool):
        return events[event_index(kind, tool)]["payload"]["duration_ms"]

    lead_begin = event_index("tool_invocation_started", "lead_probe")
    lead_end = event_index("tool_execution_finished", "lead_probe")
    delegate_end = event_index("tool_execution_finished", "delegate_tasks")
    worker_begin = next(timestamp(e) for e in child if e["event_type"] == "llm_call_started")
    worker_end = next(timestamp(e) for e in child if e["event_type"] == "llm_call_finished")
    overlap = max(0, min(worker_end, timestamp(events[lead_end])) - max(worker_begin, timestamp(events[lead_begin])))
    if background:
        assert receipt["status"] == "queued" and "summary" not in receipt
        assert observed["worker_status"][0]["status"] in {"queued", "running"}
        assert not observed["worker_wait"]["workspace_changed"]
        assert delegate_end < lead_begin < event_index("worker_finished") < lead_end < event_index("worker_result_read")
        assert all(result_text not in message.content for call in lead.received_messages[:3] for message in call)
        assert any(result_text in message.content for message in lead.received_messages[3])
        assert lead_end < event_index("worker_result_delivered") < event_index("worker_result_read")
        assert receipt["token_limit"] == 20_000 and receipt["used_tokens"] == 0
        assert overlap > 0
    else:
        assert worker_end <= timestamp(events[delegate_end]) <= timestamp(events[lead_begin])
        assert overlap == 0
    record_property("lead_worker_overlap_ms", round(overlap * 1000))
    record_property("worker_dispatch_ms", round(duration("tool_execution_finished", "delegate_tasks")))
    record_property("worker_wait_ms", round(duration("tool_execution_finished", "worker_wait")) if background else 0)
    record_property("worker_result_delivered", int(background))
    record_property("worker_result_read", int(background))


def test_background_workers_overlap_lead_and_deliver_result(tmp_path, record_property):
    _run_comparison(tmp_path, "background", record_property)


def test_sync_worker_comparison_blocks_lead_until_result(tmp_path, record_property):
    _run_comparison(tmp_path, "sync", record_property)


def test_single_agent_comparison(tmp_path, record_property):
    _run_comparison(tmp_path, "single", record_property)


def _run_partial_report(tmp_path, record_property, *, timed_out):
    from agent.coordinator import AgentCoordinator, WorkerRequest
    from entry.cli import _build_run_registry
    from task.engine import TaskEngine
    from llm.usage import TokenUsage

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.txt").write_text("deadline evidence", encoding="utf-8")
    adapting, release, reported = (threading.Event() for _ in range(3))

    class Worker(MockBackend):
        def complete(self, messages, tools):
            if self.call_count:
                adapting.set()
                assert release.wait(10)
                if not timed_out:
                    assert tools == []
            result = super().complete(messages, tools)
            if self.call_count == 1 and not timed_out:
                result.usage = TokenUsage(input_tokens=9_500, output_tokens=25)
            return result

    worker = Worker([_call("file_read", path="sample.txt"), _finish()])
    coordinator = AgentCoordinator(
        repo_path=str(repo), log_dir=str(tmp_path / "logs"), parent_task_id="lead",
        backend_factory=lambda _: worker, registry_factory=_build_run_registry,
        agent_config=AgentConfig(worker_mode="background", repo_map_mode="none"),
        engine=TaskEngine(tmp_path / "tasks.db"), timeout_seconds=1.5 if timed_out else 20,
    )
    background = coordinator.background
    with EventLog.create(Task("review", str(repo)), log_dir=str(tmp_path / "logs")) as log:
        log.on_append(lambda event: reported.set() if event.event_type == EventType.WORKER_PARTIAL_READY else None)
        background.attach(log)
        try:
            receipt = background.submit([WorkerRequest("explorer", "Find evidence.")])[0]
            assert adapting.wait(10) and reported.wait(10)
            assert worker.call_count == 1
            report = background.wait(receipt["task_id"], 0)["result"]
            assert report["termination_reason"] == ("timeout" if timed_out else "budget_wrapup")
            assert "deadline evidence" in report["evidence"][0]["excerpt"]
            notice = background.ready_notices(set())[0]
            assert notice["status"] == ("timed_out" if timed_out else "incomplete")
            if not timed_out:
                release.set()
                assert background.wait(receipt["task_id"], 10)["result"]["status"] == "success"
                assert background.ready_notices({notice["receipt_id"]})[0]["status"] == "success"
        finally:
            release.set()
            background.close()
        assert coordinator.worker_count == 1 and coordinator.usage.total_tokens == (300 if timed_out else 9_675)
        record_property("worker_evidence_count", len(report["evidence"]))
        record_property("worker_tokens", coordinator.usage.total_tokens)


def test_background_deadline_pushes_evidence_while_provider_is_still_running(tmp_path, record_property):
    _run_partial_report(tmp_path, record_property, timed_out=True)


def test_background_wrapup_pushes_partial_before_final_answer(tmp_path, record_property):
    _run_partial_report(tmp_path, record_property, timed_out=False)


def test_background_worker_resume_restores_result_and_usage(tmp_path, monkeypatch, record_property):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "fixture"], ["config", "user.email", "fixture@example.invalid"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "sample.txt").write_text("fixture\n")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    worker = MockBackend([_finish("saved review")])
    wait_call = _call("worker_wait", task_id="assigned after dispatch")
    lead = MockBackend([_delegate(), wait_call, _finish()])
    reads = []

    def observe(event):
        if event.event_type == EventType.OBSERVATION:
            obs = event.payload["observation"]
            if obs["tool_name"] == "delegate_tasks":
                wait_call.tool_call.params["task_id"] = json.loads(obs["output"])[0]["task_id"]
            elif obs["tool_name"] == "worker_wait":
                reads.append(json.loads(obs["output"]))

    def run(resume):
        runner = ExecutionRunner(
            backend=lead, registry=ToolRegistry(), worker_backend_factory=lambda role: worker,
            config=AgentConfig(multi_agent_enabled=True, worker_mode="background", repo_map_mode="none",
                               durable_runtime_config=DurableRuntimeConfig(True, str(tmp_path / "runtime"))),
            log_dir=str(tmp_path / "logs"),
        )
        try:
            return runner.run(RunRequest(task, run_id="parent", resume=resume), on_event=observe)
        finally:
            runner.close()

    task = Task("Review the fixture.", str(repo))
    from agent.background_workers import WorkerWaitTool

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as cut:
        cut.setattr(WorkerWaitTool, "execute", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(False)
    result = run(True)
    assert result.is_success()
    assert lead.call_count == 3 and worker.call_count == 1
    assert result.worker_usage.total_tokens == 150
    assert len(reads) == 1 and reads[0]["result"]["summary"] == "saved review"
    assert reads[0]["task_id"] == wait_call.tool_call.params["task_id"]
    record_property("checkpoint_recovery", 1)
    record_property("worker_model_calls", worker.call_count)


def test_background_worker_pool_queues_and_cancels_without_starting_next_job(tmp_path):
    from agent.coordinator import AgentCoordinator, WorkerRequest
    from entry.cli import _build_run_registry
    from task.engine import TaskEngine

    repo = tmp_path / "repo"
    repo.mkdir()
    started, release = threading.Event(), threading.Event()
    launches = []

    class Worker(MockBackend):
        def complete(self, *args):
            started.set()
            assert release.wait(10)
            return super().complete(*args)

    def backend_factory(role):
        launches.append(role)
        return Worker([_finish()])

    with TaskEngine(tmp_path / "tasks.db") as engine, EventLog.create(
        Task("Review", str(repo)), str(tmp_path / "logs")
    ) as log:
        coordinator = AgentCoordinator(
            repo_path=str(repo), log_dir=str(tmp_path / "logs"), parent_task_id="lead",
            backend_factory=backend_factory, registry_factory=_build_run_registry,
            agent_config=AgentConfig(worker_mode="background", repo_map_mode="none"), engine=engine,
            max_workers=1, max_tasks=2,
        )
        registry = ToolRegistry()
        coordinator.register_tools(registry)
        assert "at most 2 tasks per call" in registry.get_tool("delegate_tasks").description
        pool = coordinator.background
        pool.attach(log)
        jobs = pool.submit([WorkerRequest("explorer", "One"), WorkerRequest("reviewer", "Two")])
        running, queued = [job["task_id"] for job in jobs]
        try:
            assert started.wait(10)
            assert pool.wait(running, 0)["status"] == "running"
            assert pool.status(queued)[0]["status"] == "queued"
            with pytest.raises(ValueError, match="non-negative"):
                pool.wait(running, -1)
            # Start teardown, then release the in-flight call after queued work is canceled.
            shutdown = threading.Thread(target=pool.close)
            shutdown.start()
            with pool.condition:
                assert pool.condition.wait_for(lambda: pool.jobs[queued].result is not None, 10)
        finally:
            release.set()
            if "shutdown" in locals():
                shutdown.join(10)
                assert not shutdown.is_alive()
            else:
                pool.close()
        assert launches == ["explorer"]
        assert [job["status"] for job in pool.status(None)] == ["canceled", "canceled"]


def test_background_worker_flags_stale_result_after_lead_changes_repo(tmp_path):
    from agent.coordinator import AgentCoordinator, WorkerRequest
    from entry.cli import _build_run_registry
    from task.engine import TaskEngine

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("old")
    with TaskEngine(tmp_path / "tasks.db") as engine, EventLog.create(
        Task("Review", str(repo)), str(tmp_path / "logs")
    ) as log:
        coordinator = AgentCoordinator(
            repo_path=str(repo), log_dir=str(tmp_path / "logs"), parent_task_id="lead",
            backend_factory=lambda _: MockBackend([_finish("reviewed")]), registry_factory=_build_run_registry,
            agent_config=AgentConfig(worker_mode="background", repo_map_mode="none"), engine=engine,
        )
        pool = coordinator.background
        pool.attach(log)
        try:
            task_id = pool.submit([WorkerRequest("reviewer", "Read sample.txt")])[0]["task_id"]
            assert pool.wait(task_id, 10)["workspace_changed"] is False
            sample.write_text("new")
            assert pool.wait(task_id, 0)["workspace_changed"] is True
        finally:
            pool.close()
