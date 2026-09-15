"""Regression tests for completion verification and infrastructure aborts."""

from types import SimpleNamespace

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import (
    Action, ActionType, EventType, RunStatus, Task, ToolCall, infer_completion_requirements,
)
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


def _run(tmp_path, script, registry, config=None, *, require_changes=False, require_tests=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    task = Task(
        description="test completion guards",
        repo_path=str(repo),
        max_steps=len(script),
        require_changes=require_changes,
        require_tests=require_tests,
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


def test_finish_rejected_when_required_write_and_test_never_run(tmp_path):
    result, _, events = _run(
        tmp_path,
        [_finish_action()],
        ToolRegistry(),
        require_changes=True,
        require_tests=True,
    )

    assert result.status == RunStatus.FAILED
    assert "no write tool" in (result.error or "")
    assert events[-1].event_type == EventType.TASK_FAILED


def test_finish_rejected_when_required_test_never_run(tmp_path):
    registry = ToolRegistry().register(NoopTool("file_write", "written"))
    result, _, _ = _run(
        tmp_path,
        [_tool_action("file_write"), _finish_action()],
        registry,
        require_changes=True,
        require_tests=True,
    )

    assert result.status == RunStatus.FAILED
    assert "no test tool" in (result.error or "")


def test_infer_completion_requirements_for_coding_task():
    assert infer_completion_requirements(
        "修复 calculator.py，运行 pytest 验证"
    ) == (True, True)
    assert infer_completion_requirements(
        "只读取 README.md，不修改任何文件"
    ) == (False, False)


def test_openai_stop_text_action_is_recovered_as_tool_call():
    from llm.openai_compat import _parse_openai_response

    choice = SimpleNamespace(
        finish_reason="stop",
        message=SimpleNamespace(
            content=(
                'Thought: inspect the bug\n'
                'Action: file_write\n'
                'Params: {"path": "calculator.py", "content": "fixed"}'
            ),
            tool_calls=None,
        ),
    )
    action = _parse_openai_response(choice, choice.message.content)
    assert action.action_type == ActionType.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.name == "file_write"
    assert action.tool_call.params["path"] == "calculator.py"
