import json
import threading

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog, summarize_run
from agent.runner import (
    AcceptanceContract,
    ExecutionRunner,
    RunRequest,
    _resolve_entrypoint,
)
from agent.task import (
    Action,
    ActionType,
    EventType,
    Observation,
    ObservationStatus,
    RunResult,
    RunStatus,
    Task,
    ToolCall,
)
from agent.trace_v2 import REDACTED, TRACE_SCHEMA_VERSION, bind_trace_context
from context.history import ConversationHistory
from harness import HookEvent, Hooks
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


def _events_by_type(events):
    result = {}
    for event in events:
        result.setdefault(event.event_type, []).append(event)
    return result


def _read_trace(path):
    log = EventLog.open_existing(path)
    try:
        return log.replay()
    finally:
        log.close()


def test_trace_v2_records_prepare_llm_tool_and_configured_hooks(tmp_path):
    hook_calls = []
    hooks = Hooks().register(
        HookEvent.PRE_TOOL_USE,
        lambda block: hook_calls.append(block.name),
    )
    hooks.register(
        HookEvent.POST_TOOL_USE,
        lambda _block, _result: (_ for _ in ()).throw(RuntimeError("post failed")),
    )
    backend = MockBackend(
        [
            Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
            Action(ActionType.FINISH, "done", message="Done."),
        ],
        input_tokens=20,
        cached_tokens=5,
        output_tokens=4,
        reasoning_tokens=2,
    )
    registry = ToolRegistry().register(NoopTool("noop"))
    task = Task(
        description="Trace one turn.",
        repo_path=str(tmp_path),
        task_id="trace-v2",
        max_steps=2,
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"), entrypoint="cli")

    result = Agent(
        backend,
        registry,
        AgentConfig(hooks=hooks, prepare_next_turn=lambda _context: None),
    ).run(task, log)
    events = log.replay()

    assert result.status is RunStatus.SUCCESS
    assert hook_calls == ["noop"]

    by_type = _events_by_type(events)
    assert len(by_type[EventType.LLM_CALL_FINISHED]) == 2
    assert len(by_type[EventType.TOOL_EXECUTION_FINISHED]) == 1
    assert len(by_type[EventType.PREPARE_NEXT_TURN_FINISHED]) == 1

    run_start = by_type[EventType.TASK_START][0].payload
    run_id = run_start["run_id"]
    run_span_id = run_start["run_span_id"]
    assert run_start["trace_schema_version"] == TRACE_SCHEMA_VERSION
    assert run_start["schema_version"] == TRACE_SCHEMA_VERSION
    assert run_start["span_id"] == run_span_id
    assert run_start["parent_span_id"] is None
    assert run_start["entrypoint"] == "cli"

    llm_started = by_type[EventType.LLM_CALL_STARTED][0].payload
    llm_finished = by_type[EventType.LLM_CALL_FINISHED][0].payload
    assert llm_started["run_id"] == run_id
    assert llm_started["parent_span_id"] == run_span_id
    assert llm_started["span_type"] == "model"
    assert llm_started["span_id"] == llm_finished["span_id"]
    assert llm_started["model_call_id"] == llm_started["span_id"]
    assert llm_finished["provider_usage"]["input_tokens"] == 20
    assert llm_finished["provider_usage"]["cached_tokens"] == 5
    assert llm_finished["provider_usage"]["total_tokens"] == 24
    # Compatibility alias for existing B1/B2 readers remains available.
    assert llm_finished["usage"] == llm_finished["provider_usage"]

    breakdown = llm_started["token_breakdown"]
    assert set(breakdown) == {
        "system_tokens",
        "tool_schema_tokens",
        "repo_map_tokens",
        "planning_tokens",
        "history_tokens",
        "context_tokens",
        "pending_tokens",
        "injected_tokens",
        "estimated_input_tokens",
    }
    assert breakdown["estimated_input_tokens"] == (
        breakdown["system_tokens"]
        + breakdown["tool_schema_tokens"]
        + breakdown["repo_map_tokens"]
        + breakdown["planning_tokens"]
        + breakdown["context_tokens"]
    )
    # This test uses the default planning_mode=off, so the new diagnostic
    # field must preserve the baseline request accounting exactly.
    assert breakdown["planning_tokens"] == 0
    assert breakdown["context_tokens"] == (
        breakdown["history_tokens"]
        + breakdown["pending_tokens"]
        + breakdown["injected_tokens"]
    )
    assert breakdown["estimated_input_tokens"] > 0

    tool_started = by_type[EventType.TOOL_EXECUTION_STARTED][0].payload
    tool_finished = by_type[EventType.TOOL_EXECUTION_FINISHED][0].payload
    assert tool_started["span_id"] == tool_finished["span_id"]
    assert tool_started["parent_span_id"] == run_span_id
    assert tool_started["tool_execution_id"] == tool_started["span_id"]
    assert tool_started["arguments"] == {}
    assert tool_finished["diagnostics"] == ["post_tool_hook:RuntimeError"]

    for event in events:
        assert event.payload["trace_schema_version"] == TRACE_SCHEMA_VERSION
        assert event.payload["schema_version"] == TRACE_SCHEMA_VERSION
        assert event.payload["run_id"] == run_id
        assert event.payload["run_span_id"] == run_span_id
        assert event.payload["entrypoint"] == "cli"

    for event_type in (
        EventType.PREPARE_NEXT_TURN_FINISHED,
        EventType.LLM_CALL_FINISHED,
        EventType.TOOL_EXECUTION_FINISHED,
    ):
        payload = by_type[event_type][0].payload
        assert payload["turn_id"]
        assert payload["span_id"]
        assert payload["duration_ms"] >= 0

    summary = summarize_run(log)
    assert summary["usage"]["input_tokens"] == 40
    assert summary["usage"]["output_tokens"] == 8
    assert summary["trace"]["prepare_calls"] == 1
    assert summary["trace"]["llm_calls"] == 2
    assert summary["trace"]["tool_calls"] == 1
    log.close()


def test_nested_schema_redaction_happens_at_jsonl_boundary_and_keeps_usage(tmp_path):
    task = Task("redact", str(tmp_path), task_id="redaction")
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"), entrypoint="api")
    log.log_task_start(task)
    action = Action(
        ActionType.TOOL_CALL,
        "use credentials",
        ToolCall(
            "noop",
            {
                "authorization": "Bearer authorization-secret-123",
                "nested": [
                    {"api_key": "sk-supersecret123456"},
                    {"input_tokens": 123, "token": "plain-secret-token"},
                    {"note": "github_pat_abcdefghijklmnop"},
                ],
                "proxy-authorization": "Bearer proxy-secret-123",
            },
        ),
    )
    log.log_action(step=1, action=action, raw_content="ghp_abcdefghijklmnop")
    log.log_observation(
        1,
        Observation(
            ObservationStatus.ERROR,
            output="Authorization: Bearer output-secret-123",
            tool_name="noop",
            error="github_token=ghp_qwertyuiopasdfgh password=hunter2",
        ),
    )
    log.log_permission_decision(
        task.task_id,
        "noop",
        "deny",
        "secret=permission-secret",
        {
            "refresh_token": "refresh-secret",
            "usage": {"input_tokens": 77, "output_tokens": 4},
        },
    )
    log.log_task_failed(
        1,
        "provider error Authorization: Bearer final-secret-123 sk-anothersecret12345",
    )
    log.close()

    raw = log.path.read_text(encoding="utf-8")
    for secret in (
        "authorization-secret-123",
        "proxy-secret-123",
        "supersecret123456",
        "plain-secret-token",
        "abcdefghijklmnop",
        "output-secret-123",
        "qwertyuiopasdfgh",
        "hunter2",
        "refresh-secret",
        "permission-secret",
        "final-secret-123",
        "anothersecret12345",
    ):
        assert secret not in raw
    assert REDACTED in raw

    events = log.replay()
    action_payload = next(
        event.payload for event in events if event.event_type is EventType.ACTION
    )
    params = action_payload["action"]["tool_call"]["params"]
    assert params["authorization"] == REDACTED
    assert params["nested"][0]["api_key"] == REDACTED
    assert params["nested"][1]["token"] == REDACTED
    assert params["nested"][1]["input_tokens"] == 123

    permission = next(
        event.payload
        for event in events
        if event.event_type is EventType.PERMISSION_DECISION
    )
    assert permission["params"]["refresh_token"] == REDACTED
    assert permission["params"]["usage"] == {"input_tokens": 77, "output_tokens": 4}


def test_context_compaction_span_does_not_reuse_parent_prepare_span(tmp_path):
    task = Task("compact", str(tmp_path), task_id="compact-span")
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    log.log_task_start(task)
    prepare_span = log.log_trace(EventType.PREPARE_NEXT_TURN_STARTED, 2)
    compaction_span = log.log_trace(
        EventType.CONTEXT_COMPACTION_STARTED,
        2,
        pressure=0.9,
        before_tokens=900,
    )
    log.log_trace(
        EventType.CONTEXT_COMPACTED,
        2,
        pressure=0.9,
        before_tokens=900,
        after_tokens=500,
        method="deterministic",
    )
    log.log_trace(
        EventType.PREPARE_NEXT_TURN_FINISHED,
        2,
        span_id=prepare_span,
        duration_ms=1.0,
    )
    events = log.replay()
    by_type = _events_by_type(events)

    compacted = by_type[EventType.CONTEXT_COMPACTED][0].payload
    assert compaction_span != prepare_span
    assert compacted["span_id"] == compaction_span
    assert compacted["compaction_id"] == compaction_span
    assert compacted["parent_span_id"] == log.run_span_id
    assert compacted["span_type"] == "context"
    log.close()


def test_completion_rejection_and_incomplete_are_correlated_and_runner_terminates(tmp_path):
    task = Task(
        "must test",
        str(tmp_path),
        task_id="completion-rejected",
        max_steps=1,
        require_tests=True,
    )
    runner = ExecutionRunner(
        backend=MockBackend([Action(ActionType.FINISH, "done", message="Done.")]),
        registry=ToolRegistry(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(task=task, entrypoint="cli"))
    events = _read_trace(result.trace_path)
    by_type = _events_by_type(events)

    assert result.status is RunStatus.INCOMPLETE
    assert result.termination_reason == "resource_exhausted"
    assert result.resource_reason == "max_steps"
    rejection = by_type[EventType.COMPLETION_REJECTED][0].payload
    assert rejection["status"] == "rejected"
    assert rejection["span_type"] == "completion"
    assert rejection["parent_span_id"] == rejection["run_span_id"]
    incomplete = by_type[EventType.TASK_INCOMPLETE][0].payload
    assert incomplete["termination_reason"] == "resource_exhausted"
    assert incomplete["resource_reason"] == "max_steps"
    termination = by_type[EventType.RUN_TERMINATED][0].payload
    assert termination["status"] == "incomplete"
    assert termination["termination_reason"] == "resource_exhausted"
    assert termination["resource_reason"] == "max_steps"


def test_provider_error_trace_is_redacted_and_normalized_at_run_termination(tmp_path):
    secret = "provider-secret-123456"

    class FailingBackend(MockBackend):
        def __init__(self):
            super().__init__([])

        def complete(self, messages, tools):
            raise RuntimeError(f"401 Authorization: Bearer {secret}")

    task = Task("provider fail", str(tmp_path), task_id="provider-error", max_steps=1)
    runner = ExecutionRunner(
        backend=FailingBackend(),
        registry=ToolRegistry(),
        config=AgentConfig(llm_max_retries=1),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(task=task, entrypoint="api"))
    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "provider_error"
    raw = (tmp_path / "logs" / (result.trace_path.split("/")[-1])).read_text(
        encoding="utf-8"
    ) if False else open(result.trace_path, encoding="utf-8").read()
    assert secret not in raw

    events = _read_trace(result.trace_path)
    failed = next(
        event.payload for event in events if event.event_type is EventType.LLM_CALL_FAILED
    )
    termination = next(
        event.payload for event in events if event.event_type is EventType.RUN_TERMINATED
    )
    assert failed["status"] == "error"
    assert REDACTED in failed["error"]
    assert termination["termination_reason"] == "provider_error"
    assert REDACTED in termination["error"]


def test_cancel_trace_uses_api_entrypoint_and_terminal_reason(tmp_path):
    cancel = threading.Event()
    cancel.set()
    task = Task("cancel", str(tmp_path), task_id="cancel-trace", max_steps=1)
    runner = ExecutionRunner(
        backend=MockBackend([]),
        registry=ToolRegistry(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(task=task, cancel_event=cancel))
    events = _read_trace(result.trace_path)
    termination = next(
        event.payload for event in events if event.event_type is EventType.RUN_TERMINATED
    )
    assert result.status is RunStatus.CANCELED
    assert termination["termination_reason"] == "canceled"
    assert termination["entrypoint"] == "api"


def test_acceptance_and_delivery_trace_statuses(tmp_path):
    task = Task("lifecycle", str(tmp_path), task_id="lifecycle")
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"), entrypoint="github_issue")
    log.log_task_start(task)
    result = RunResult(
        task_id=task.task_id,
        status=RunStatus.SUCCESS,
        summary="done",
        steps_taken=1,
        acceptance_status="passed",
        delivery_status="delivered",
        termination_reason="completion_satisfied",
    )
    log.log_acceptance(result, requested=True)
    log.log_run_termination(result)
    log.log_delivery(
        steps=1,
        requested=True,
        delivery_status="delivered",
        repo="owner/repo",
    )
    events = log.replay()
    by_type = _events_by_type(events)

    acceptance = by_type[EventType.ACCEPTANCE][0].payload
    delivery = by_type[EventType.DELIVERY][0].payload
    termination = by_type[EventType.RUN_TERMINATED][0].payload
    assert acceptance["requested"] is True
    assert acceptance["status"] == "passed"
    assert delivery["requested"] is True
    assert delivery["status"] == "delivered"
    assert termination["termination_reason"] == "completion_satisfied"
    assert all(
        payload["parent_span_id"] == log.run_span_id
        for payload in (acceptance, delivery)
    )
    log.close()


@pytest.mark.parametrize(
    ("run_request", "expected"),
    [
        (RunRequest(Task("cli", ".")), "cli"),
        (RunRequest(Task("chat", "."), history=ConversationHistory()), "chat"),
        (RunRequest(Task("api", "."), cancel_event=threading.Event()), "api"),
        (
            RunRequest(Task("issue", ".", issue_url="https://github.com/o/r/issues/1")),
            "github_issue",
        ),
    ],
)
def test_four_entrypoints_resolve_to_one_trace_schema(run_request, expected):
    assert _resolve_entrypoint(run_request) == expected


def test_trace_context_propagates_into_nested_event_log_create(tmp_path):
    task = Task("nested", str(tmp_path), task_id="nested-context")
    with bind_trace_context(entrypoint="api", session_id="api-session"):
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    log.log_task_start(task)
    payload = log.replay()[0].payload
    assert payload["entrypoint"] == "api"
    assert payload["session_id"] == "api-session"
    log.close()


def test_old_jsonl_replays_without_schema_v2_and_new_append_does_not_rewrite_it(tmp_path):
    path = tmp_path / "legacy.jsonl"
    legacy = {
        "event_id": "old-event",
        "event_type": "task_start",
        "task_id": "legacy-task",
        "timestamp": "2026-07-01T00:00:00+00:00",
        "payload": {"task": {"description": "old"}},
    }
    original = json.dumps(legacy, ensure_ascii=False)
    path.write_text(original + "\n", encoding="utf-8")

    log = EventLog.open_existing(path, task_id="legacy-task", entrypoint="cli")
    before = log.replay()
    assert before[0].payload == legacy["payload"]
    assert "trace_schema_version" not in before[0].payload

    log.log_task_failed(0, "new failure")
    log.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == original
    appended = json.loads(lines[1])
    assert appended["payload"]["trace_schema_version"] == TRACE_SCHEMA_VERSION
    assert appended["payload"]["schema_version"] == TRACE_SCHEMA_VERSION


def test_trace_writer_failure_does_not_replace_agent_result(tmp_path, caplog):
    backend = MockBackend([Action(ActionType.FINISH, "done", message="Done.")])
    registry = ToolRegistry()
    task = Task(description="Finish.", repo_path=str(tmp_path), task_id="trace-failure")
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    append = log._append

    def fail_trace_only(event):
        if event.event_type.name.startswith("LLM_CALL_"):
            raise OSError("trace disk failed")
        append(event)

    log._append = fail_trace_only
    with caplog.at_level("WARNING"):
        result = Agent(backend, registry).run(task, log)

    assert result.status is RunStatus.SUCCESS
    assert "Trace write failed" in caplog.text
    log.close()
