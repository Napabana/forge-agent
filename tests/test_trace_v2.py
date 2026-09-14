from agent.core import Agent, AgentConfig
from agent.event_log import EventLog, summarize_run
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from harness import HookEvent, Hooks
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


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
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    result = Agent(
        backend,
        registry,
        AgentConfig(hooks=hooks, prepare_next_turn=lambda _context: None),
    ).run(task, log)
    events = log.replay()

    assert result.status is RunStatus.SUCCESS
    assert hook_calls == ["noop"]

    by_type = {}
    for event in events:
        by_type.setdefault(event.event_type, []).append(event)

    assert len(by_type[EventType.LLM_CALL_FINISHED]) == 2
    assert len(by_type[EventType.TOOL_EXECUTION_FINISHED]) == 1
    assert len(by_type[EventType.PREPARE_NEXT_TURN_FINISHED]) == 1

    llm_finished = by_type[EventType.LLM_CALL_FINISHED][0].payload
    assert llm_finished["usage"]["input_tokens"] == 20
    assert llm_finished["usage"]["cached_tokens"] == 5
    assert llm_finished["usage"]["total_tokens"] == 24
    assert by_type[EventType.TOOL_EXECUTION_FINISHED][0].payload["diagnostics"] == [
        "post_tool_hook:RuntimeError"
    ]

    for event_type in (
        EventType.PREPARE_NEXT_TURN_FINISHED,
        EventType.LLM_CALL_FINISHED,
        EventType.TOOL_EXECUTION_FINISHED,
    ):
        payload = by_type[event_type][0].payload
        assert payload["schema_version"] == 2
        assert payload["run_id"]
        assert payload["turn_id"]
        assert payload["span_id"]
        assert payload["duration_ms"] >= 0

    summary = summarize_run(log)
    assert summary["usage"]["input_tokens"] == 40
    assert summary["usage"]["output_tokens"] == 8
    assert summary["trace"]["prepare_calls"] == 1
    assert summary["trace"]["llm_calls"] == 2
    assert summary["trace"]["tool_calls"] == 1


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
