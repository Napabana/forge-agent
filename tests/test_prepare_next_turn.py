from threading import Event as CancelEvent

from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from tools.base import NoopTool, ToolRegistry
from agent.core import (
    Agent,
    AgentConfig,
    PrepareNextTurnResult,
)
from llm.base import LLMMessage, MockBackend


def _two_turn_backend():
    return MockBackend([
        Action(ActionType.TOOL_CALL, "Run a tool first.", ToolCall("noop", {})),
        Action(ActionType.FINISH, "Finished.", message="Done."),
    ])


def _task(tmp_path, task_id):
    return Task(
        task_id=task_id,
        description="Inspect the repository.",
        repo_path=str(tmp_path),
        max_steps=3,
    )


def test_prepare_next_turn_skips_first_turn_and_runs_before_second(tmp_path):
    prepare_steps = []

    def prepare_next_turn(context):
        prepare_steps.append(context.step)
        return None

    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="Run a tool first.",
            tool_call=ToolCall(name="noop", params={}),
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="Finished.",
            message="Done.",
        ),
    ])

    registry = ToolRegistry()
    registry.register(NoopTool("noop"))

    task = Task(
        task_id="prepare-test",
        description="Inspect the repository.",
        repo_path=str(tmp_path),
        max_steps=3,
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    agent = Agent(
        backend,
        registry,
        AgentConfig(prepare_next_turn=prepare_next_turn),
    )
    result = agent.run(task, log)

    assert result.status == RunStatus.SUCCESS
    assert backend.call_count == 2
    assert prepare_steps == [2]


def test_prepare_next_turn_injects_message_before_second_llm_call(tmp_path):
    injected_text = "State injected before the next turn."

    def prepare_next_turn(_context):
        return PrepareNextTurnResult(
            messages=(
                LLMMessage(
                    role="user",
                    content=injected_text,
                ),
            ),
        )

    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="Run a tool first.",
            tool_call=ToolCall(name="noop", params={}),
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="Finished.",
            message="Done.",
        ),
    ])

    registry = ToolRegistry()
    registry.register(NoopTool("noop"))

    task = Task(
        task_id="prepare-injection",
        description="Inspect the repository.",
        repo_path=str(tmp_path),
        max_steps=3,
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    agent = Agent(
        backend,
        registry,
        AgentConfig(prepare_next_turn=prepare_next_turn),
    )
    result = agent.run(task, log)

    first_request = backend.received_messages[0]
    second_request = backend.received_messages[1]

    assert result.status == RunStatus.SUCCESS
    assert all(message.content != injected_text for message in first_request)
    assert any(message.content == injected_text for message in second_request)


def test_prepare_next_turn_default_none_preserves_messages(tmp_path):
    task = _task(tmp_path, "prepare-default")
    backends = [_two_turn_backend(), _two_turn_backend()]

    for backend, config, log_name in (
        (backends[0], AgentConfig(), "default"),
        (backends[1], AgentConfig(prepare_next_turn=lambda _context: None), "callback"),
    ):
        Agent(backend, ToolRegistry().register(NoopTool("noop")), config).run(
            task,
            EventLog.create(task, log_dir=str(tmp_path / log_name)),
        )

    assert [messages[1:] for messages in backends[0].received_messages] == [
        messages[1:] for messages in backends[1].received_messages
    ]


def test_prepare_next_turn_exception_stops_before_next_llm_and_is_traced(tmp_path):
    def fail(_context):
        raise ValueError("bad preparation")

    task = _task(tmp_path, "prepare-error")
    backend = _two_turn_backend()
    log = EventLog.create(task, log_dir=str(tmp_path / "error"))
    result = Agent(
        backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(prepare_next_turn=fail),
    ).run(task, log)

    failed = [e for e in log.replay() if e.event_type is EventType.PREPARE_NEXT_TURN_FAILED]
    assert result.status is RunStatus.FAILED
    assert backend.call_count == 1
    assert failed[0].payload["error_type"] == "ValueError"


def test_prepare_next_turn_honors_cancel_before_and_during_callback(tmp_path):
    canceled_before = CancelEvent()
    canceled_before.set()
    before_calls = []
    before_backend = _two_turn_backend()
    before_task = _task(tmp_path, "cancel-before")
    before_result = Agent(
        before_backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(
            cancel_event=canceled_before,
            prepare_next_turn=lambda context: before_calls.append(context.step),
        ),
    ).run(before_task, EventLog.create(before_task, log_dir=str(tmp_path / "before")))

    canceled_during = CancelEvent()
    during_calls = []

    def cancel_during(context):
        during_calls.append(context.step)
        canceled_during.set()

    during_backend = _two_turn_backend()
    during_task = _task(tmp_path, "cancel-during")
    during_result = Agent(
        during_backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(cancel_event=canceled_during, prepare_next_turn=cancel_during),
    ).run(during_task, EventLog.create(during_task, log_dir=str(tmp_path / "during")))

    assert before_result.status is RunStatus.CANCELED
    assert before_calls == [] and before_backend.call_count == 0
    assert during_result.status is RunStatus.CANCELED
    assert during_calls == [2] and during_backend.call_count == 1
