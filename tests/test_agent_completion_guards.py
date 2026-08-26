"""Regression tests for completion verification and infrastructure aborts."""

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from llm.base import MockBackend
from tools.base import FailingTool, NoopTool, ToolRegistry


def _tool_action(name: str) -> Action:
    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=f"call {name}",
        tool_call=ToolCall(name=name, params={}),
    )


def _finish_action() -> Action:
    return Action(
        action_type=ActionType.FINISH,
        thought="done",
        message="done",
    )


def _run(tmp_path, script, registry, config=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    task = Task(
        description="test completion guards",
        repo_path=str(repo),
        max_steps=len(script),
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    backend = MockBackend(script)
    try:
        result = Agent(
            backend,
            registry,
            config=config or AgentConfig(),
        ).run(task, log)
    finally:
        log.close()
    return result, backend, list(log.replay())


def test_finish_rejected_after_failed_test(tmp_path):
    registry = ToolRegistry().register(FailingTool("test", "2 failed"))
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.FAILED
    assert "latest test did not pass" in (result.error or "")
    assert events[-1].event_type == EventType.TASK_FAILED


def test_finish_allowed_after_successful_test(tmp_path):
    registry = ToolRegistry().register(NoopTool("test", "2 passed"))
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.SUCCESS
    assert events[-1].event_type == EventType.TASK_COMPLETE


def test_finish_rejected_after_single_fatal_infrastructure_error(tmp_path):
    error = "Failed to start container: Duplicate mount point: /workspace"
    registry = ToolRegistry().register(FailingTool("shell", error))
    result, backend, events = _run(
        tmp_path,
        [_tool_action("shell"), _finish_action()],
        registry,
        AgentConfig(fatal_tool_error_repeats=2),
    )

    assert result.status == RunStatus.FAILED
    assert "unresolved fatal infrastructure error" in (result.error or "").lower()
    assert backend.call_count == 2
    assert events[-1].event_type == EventType.TASK_FAILED


def test_finish_rejected_when_write_follows_successful_test(tmp_path):
    registry = (
        ToolRegistry()
        .register(NoopTool("test", "2 passed"))
        .register(NoopTool("file_write", "written"))
    )
    result, _, _ = _run(
        tmp_path,
        [_tool_action("test"), _tool_action("file_write"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.FAILED
    assert "final state is unverified" in (result.error or "")


def test_repeated_fatal_infrastructure_error_aborts_early(tmp_path):
    error = "Failed to start container: Duplicate mount point: /workspace"
    registry = ToolRegistry().register(FailingTool("shell", error))
    result, backend, events = _run(
        tmp_path,
        [_tool_action("shell"), _tool_action("shell"), _finish_action()],
        registry,
        AgentConfig(fatal_tool_error_repeats=2),
    )

    assert result.status == RunStatus.FAILED
    assert "duplicate mount point" in (result.error or "")
    assert result.steps_taken == 2
    assert backend.call_count == 2
    assert events[-1].event_type == EventType.TASK_FAILED
