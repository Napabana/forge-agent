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
from llm.base import LLMMessage, LLMToolSchema, MockBackend
from tools.base import NoopTool, ToolRegistry


def test_history_keeps_canonical_messages_before_context_policy():
    history = ConversationHistory(max_messages=3)
    history.add(LLMMessage("user", "original constraint"))
    for index in range(4):
        history.add(LLMMessage("assistant", f"action {index}"))
        history.add(LLMMessage("user", f"observation {index}"))

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


def test_request_pressure_counts_full_request_without_double_counting_repo_map():
    budget = TokenBudget(total=1_000)
    tools = (
        LLMToolSchema(
            name="file_read",
            description="read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
    )
    pressure = budget.request_pressure(
        system_text="system " * 80,
        repo_map_text="repo " * 40,
        history=[{"role": "user", "content": "task"}],
        tools=tools,
    )

    assert pressure.system_tokens > 0
    assert pressure.tool_schema_tokens > 0
    assert pressure.repo_map_tokens > 0
    assert pressure.history_tokens > 0
    assert pressure.projected_input == (
        pressure.system_tokens + pressure.tool_schema_tokens + pressure.history_tokens
    )
    assert pressure.available_input == 850


def _history_with_units(count: int = 5) -> ConversationHistory:
    history = ConversationHistory(max_messages=4)
    history.add(LLMMessage("user", "original acceptance constraint"))
    for index in range(count):
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
    return history


def test_compaction_keeps_canonical_history_and_returns_token_based_model_view(tmp_path):
    history = _history_with_units()
    canonical_before = history.to_dicts()
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
        system_content="system prompt",
    )

    result = strategy(context)

    assert result is not None
    assert result.history_override is not None
    assert history.to_dicts() == canonical_before
    assert not any("[Compacted earlier context" in m.content for m in history.to_list())

    checkpoint = strategy.checkpoints[0]
    override = list(result.history_override)
    assert [message.role for message in override[-4:]] == [
        "assistant", "user", "assistant", "user",
    ]
    assert [message.event_ref for message in override[-4:]] == [
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
    assert strategy.entries[0].checkpoint_id == checkpoint.checkpoint_id
    log.close()


def test_full_request_pressure_can_trigger_even_when_history_is_small(tmp_path):
    history = _history_with_units(count=2)
    strategy = TraceableCompaction(
        threshold=0.7,
        target_ratio=0.2,
        keep_recent_tokens=20,
    )
    task = Task("Continue.", str(tmp_path), task_id="pressure", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    context = PrepareNextTurnContext(
        task=task,
        step=2,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=TokenBudget(total=600),
        cancel_event=None,
        event_log=log,
        system_content="large-system " * 160,
        repo_map_content="repo-map " * 40,
        tool_schemas=(
            LLMToolSchema(
                name="noop",
                description="no operation " * 20,
                parameters={"type": "object", "properties": {}},
            ),
        ),
    )

    result = strategy(context)

    assert result is not None
    checkpoint = strategy.checkpoints[0]
    assert checkpoint.pressure_ratio >= 0.7
    assert checkpoint.projected_input_tokens > checkpoint.available_input_tokens * 0.7
    log.close()


def test_repeated_compaction_links_previous_checkpoint_without_summary_in_history(tmp_path):
    history = _history_with_units(count=4)
    strategy = TraceableCompaction(
        threshold=0.2,
        target_ratio=0.1,
        keep_recent_tokens=20,
    )
    task = Task("Continue.", str(tmp_path), task_id="repeat", max_steps=3)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    def make_context(step: int):
        return PrepareNextTurnContext(
            task=task,
            step=step,
            history=history,
            repo_map=RepoMap(tmp_path),
            token_budget=TokenBudget(total=500),
            cancel_event=None,
            event_log=log,
            system_content="system " * 80,
        )

    first = strategy(make_context(2))
    history.add(LLMMessage("assistant", "new action " + "c" * 200, event_ref="action-new"))
    history.add(LLMMessage("user", "new observation " + "d" * 200, event_ref="observation-new"))
    second = strategy(make_context(3))

    assert first is not None and second is not None
    assert len(strategy.entries) == 2
    assert strategy.entries[1].previous_checkpoint_id == strategy.entries[0].checkpoint_id
    assert strategy.checkpoints[1].previous_checkpoint_id == strategy.checkpoints[0].checkpoint_id
    assert not any("[Compacted earlier context" in m.content for m in history.to_list())
    log.close()


def test_compaction_runs_at_turn_boundary_and_only_changes_next_model_view(tmp_path):
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

    canonical_count_before_run = history.message_count
    strategy = TraceableCompaction(
        threshold=0.5,
        target_ratio=0.25,
        keep_recent_tokens=20,
    )
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
        Action(ActionType.FINISH, "done", message="Done."),
    ])
    task = Task("Continue.", str(tmp_path), task_id="compact", max_steps=2, budget_tokens=2_000)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    Agent(
        backend,
        ToolRegistry().register(NoopTool("noop")),
        AgentConfig(budget_tokens=2_000, prepare_next_turn=strategy),
    ).run(task, log, history=history)

    checkpoint = strategy.checkpoints[0]
    compacted = [
        event for event in log.replay()
        if event.event_type is EventType.CONTEXT_COMPACTED
    ]
    assert history.to_list()[0].content == "original acceptance constraint"
    assert history.message_count == canonical_count_before_run + 2
    assert not any("[Compacted earlier context" in m.content for m in history.to_list())
    second_call_contents = "\n".join(m.content for m in backend.received_messages[1])
    assert "[Compacted earlier context" in second_call_contents
    assert checkpoint.after_tokens < checkpoint.before_tokens
    assert checkpoint.source_event_ids[:2] == ("action-0", "observation-0")
    assert checkpoint.keep_recent_tokens == 20
    assert compacted[0].payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert compacted[0].payload["summary_hash"] == checkpoint.summary_hash
    assert compacted[0].payload["projected_input_tokens"] == checkpoint.projected_input_tokens


def test_chat_persists_compaction_checkpoint(tmp_path):
    config = AppConfig()
    config.agent.max_steps = 2
    config.agent.budget_tokens = 2_000
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
        session._shared_history.add(
            LLMMessage("assistant", f"old action {index} " + "x" * 80)
        )
        session._shared_history.add(
            LLMMessage("user", f"old result {index} " + "y" * 80)
        )

    assert session.run_round("continue")

    restored = store.load(session.session_id)
    checkpoint = restored.compaction_checkpoints[0]
    assert checkpoint["checkpoint_id"] == strategy.checkpoints[0].checkpoint_id
    assert checkpoint["keep_recent_tokens"] == 20
    assert checkpoint["previous_checkpoint_id"] is None
