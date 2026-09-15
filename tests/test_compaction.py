from agent.core import Agent, AgentConfig, PrepareNextTurnContext
from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, Task, ToolCall
from agent.session_store import JsonChatSessionStore
from config.schema import AppConfig
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import (
    TokenBudget,
    history_unit_tokens,
    history_units,
)
from entry.chat import ChatSession
from llm.base import LLMMessage, MockBackend
from tools.base import NoopTool, ToolRegistry


def test_history_keeps_canonical_messages_before_context_policy():
    history = ConversationHistory(max_messages=3)
    history.add(LLMMessage("user", "original constraint"))
    for index in range(4):
        history.add(LLMMessage("assistant", f"action {index}"))
        history.add(LLMMessage("user", f"observation {index}"))

    # max_messages 只保留为旧配置提示，不能在 Compaction/TokenBudget 前永久删证据。
    assert history.message_count == 9
    assert history.to_list()[1].content == "action 0"


def test_history_units_share_action_observation_pairing():
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "action 1"},
        {"role": "user", "content": "observation 1"},
        {"role": "assistant", "content": "action 2"},
        {"role": "tool", "content": "observation 2"},
        {"role": "user", "content": "standalone reflection"},
    ]

    assert [unit.indices for unit in history_units(messages)] == [
        (1, 2),
        (3, 4),
        (5,),
    ]


def test_compaction_keeps_recent_complete_units_by_token_budget(tmp_path):
    history = ConversationHistory(max_messages=4)
    history.add(LLMMessage("user", "original acceptance constraint"))
    for index in range(5):
        history.add(LLMMessage(
            "assistant",
            f"action {index} " + "a" * 200,
            event_ref=f"action-{index}",
        ))
        history.add(LLMMessage(
            "user",
            f"observation {index} " + "b" * 200,
            event_ref=f"observation-{index}",
        ))

    raw = history.to_dicts()
    units = history_units(raw)
    keep_recent_tokens = sum(history_unit_tokens(raw, unit) for unit in units[-2:])
    strategy = TraceableCompaction(
        threshold=0.2,
        target_ratio=0.1,
        keep_recent_tokens=keep_recent_tokens,
    )
    task = Task("Continue.", str(tmp_path), task_id="compact-units", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    context = PrepareNextTurnContext(
        task=task,
        step=2,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=TokenBudget(total=1_000),
        cancel_event=None,
        event_log=log,
    )

    strategy(context)

    checkpoint = strategy.checkpoints[0]
    tail = history.to_list()[-4:]
    assert [message.role for message in tail] == ["assistant", "user", "assistant", "user"]
    assert [message.event_ref for message in tail] == [
        "action-3", "observation-3", "action-4", "observation-4",
    ]
    assert checkpoint.source_event_ids == (
        "action-0", "observation-0",
        "action-1", "observation-1",
        "action-2", "observation-2",
    )
    assert checkpoint.retained_tail == 4
    assert checkpoint.keep_recent_tokens == keep_recent_tokens
    assert checkpoint.retained_tail_tokens == keep_recent_tokens
    log.close()


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

    strategy = TraceableCompaction(
        threshold=0.5,
        target_ratio=0.25,
        keep_recent_tokens=20,
    )
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
        Action(ActionType.FINISH, "done", message="Done."),
    ])
    task = Task("Continue.", str(tmp_path), task_id="compact", max_steps=2, budget_tokens=160)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    Agent(
        backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(budget_tokens=160, prepare_next_turn=strategy),
    ).run(task, log, history=history)

    checkpoint = strategy.checkpoints[0]
    compacted = [event for event in log.replay() if event.event_type is EventType.CONTEXT_COMPACTED]
    assert history.to_list()[0].content == "original acceptance constraint"
    assert checkpoint.after_tokens < checkpoint.before_tokens
    assert checkpoint.source_event_ids[:2] == ("action-0", "observation-0")
    assert checkpoint.keep_recent_tokens == 20
    assert compacted[0].payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert compacted[0].payload["summary_hash"] == checkpoint.summary_hash
    assert compacted[0].payload["keep_recent_tokens"] == 20


def test_chat_persists_compaction_checkpoint(tmp_path):
    config = AppConfig()
    config.agent.max_steps = 2
    config.agent.budget_tokens = 160
    config.agent.log_dir = str(tmp_path / "logs")
    config.context.history_window = 20
    store = JsonChatSessionStore(tmp_path / "sessions")
    strategy = TraceableCompaction(
        threshold=0.5,
        target_ratio=0.25,
        keep_recent_tokens=20,
    )
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
    checkpoint = restored.compaction_checkpoints[0]
    assert checkpoint["checkpoint_id"] == strategy.checkpoints[0].checkpoint_id
    assert checkpoint["keep_recent_tokens"] == 20
