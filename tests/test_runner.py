from agent.core import AgentConfig
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from context.history import ConversationHistory
from harness import HookEvent, Hooks
from llm.base import LLMMessage, MockBackend
from tools.base import NoopTool, ToolRegistry


def test_runner_owns_direct_lifecycle_and_preserves_history(tmp_path):
    seen = []
    hooks = Hooks().register(HookEvent.PRE_TOOL_USE, lambda block: seen.append(block.name))
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
        Action(ActionType.FINISH, "done", message="Done."),
    ])
    history = ConversationHistory()
    history.add(LLMMessage("user", "existing session context"))
    task = Task("Run it.", str(tmp_path), task_id="runner-direct", max_steps=2)
    runner = ExecutionRunner(
        backend=backend,
        registry=ToolRegistry().register(NoopTool("noop")),
        config=AgentConfig(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(task, history=history, hooks=hooks))

    assert result.status is RunStatus.SUCCESS
    assert result.trace_path and seen == ["noop"]
    assert any(message.content == "existing session context" for message in backend.received_messages[0])


def test_runner_freezes_acceptance_before_agent_run(tmp_path):
    task = Task("Finish without edits.", str(tmp_path), task_id="runner-acceptance")
    runner = ExecutionRunner(
        backend=MockBackend([Action(ActionType.FINISH, "done", message="Done.")]),
        registry=ToolRegistry(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(
        task,
        acceptance=AcceptanceContract(require_changes=True),
    ))

    assert result.status is RunStatus.FAILED
    assert "requires repository changes" in result.summary
