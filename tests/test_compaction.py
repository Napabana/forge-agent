from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, Task, ToolCall
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from config.schema import AppConfig
from entry.chat import ChatSession
from agent.session_store import JsonChatSessionStore
from llm.base import LLMMessage, MockBackend
from tools.base import NoopTool, ToolRegistry


def test_compaction_runs_at_turn_boundary_and_keeps_traceable_checkpoint(tmp_path):
    history = ConversationHistory(max_messages=40)
    history.add(LLMMessage("user", "original acceptance constraint"))
    for index in range(6):
        history.add(LLMMessage(
            "assistant",
            f"old action {index} " + "x" * 80,
            event_ref=f"action-{index}",
        ))
        history.add(LLMMessage(
            "user",
            f"old observation {index} " + "y" * 80,
            event_ref=f"observation-{index}",
        ))

    strategy = TraceableCompaction(threshold=0.5, target_ratio=0.25, retained_tail=4)
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
        Action(ActionType.FINISH, "done", message="Done."),
    ])
    task = Task("Continue.", str(tmp_path), task_id="compact", max_steps=2, budget_tokens=300)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    Agent(
        backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(budget_tokens=300, prepare_next_turn=strategy),
    ).run(task, log, history=history)

    checkpoint = strategy.checkpoints[0]
    compacted = [event for event in log.replay() if event.event_type is EventType.CONTEXT_COMPACTED]
    assert history.to_list()[0].content == "original acceptance constraint"
    assert checkpoint.after_tokens < checkpoint.before_tokens
    assert checkpoint.source_event_ids[:2] == ("action-0", "observation-0")
    assert compacted[0].payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert compacted[0].payload["summary_hash"] == checkpoint.summary_hash


def test_chat_persists_compaction_checkpoint(tmp_path):
    config = AppConfig()
    config.agent.max_steps = 2
    config.agent.budget_tokens = 300
    config.agent.log_dir = str(tmp_path / "logs")
    config.context.history_window = 20
    store = JsonChatSessionStore(tmp_path / "sessions")
    strategy = TraceableCompaction(threshold=0.5, target_ratio=0.25, retained_tail=4)
    session = ChatSession(
        backend=MockBackend([
            Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
            Action(ActionType.FINISH, "done", message="Done."),
        ]),
        registry=ToolRegistry().register(NoopTool("noop")),
        config=config,
        repo_path=str(tmp_path),
        log_dir=config.agent.log_dir,
        session_store=store,
        prepare_next_turn=strategy,
        stream=False,
    )
    for index in range(6):
        session._shared_history.add(LLMMessage("assistant", f"old action {index} " + "x" * 80))
        session._shared_history.add(LLMMessage("user", f"old result {index} " + "y" * 80))

    assert session.run_round("continue")

    restored = store.load(session.session_id)
    assert restored.compaction_checkpoints[0]["checkpoint_id"] == strategy.checkpoints[0].checkpoint_id
