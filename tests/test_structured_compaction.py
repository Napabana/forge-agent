from __future__ import annotations

from dataclasses import dataclass

from agent.core import PrepareNextTurnContext
from agent.event_log import EventLog
from agent.task import Action, ActionType, Event, EventType, Task, ToolCall
from config.schema import AppConfig
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from context.history_evidence import (
    action_fingerprint,
    iter_interactions,
    parse_action_message,
    parse_observation_message,
)
from context.repo_map import RepoMap
from context.structured_compaction import (
    DeterministicEvidence,
    LLMSemanticSummarizer,
    SemanticContextFields,
    SemanticSummaryResult,
    build_deterministic_evidence,
    build_semantic_packet,
    build_structured_state,
    render_structured_context,
)
from context.token_budget import TokenBudget
from entry.chat import ChatSession, _print_event_live
from llm.base import LLMMessage, MockBackend
from llm.usage import TokenUsage
from tools.base import NoopTool, ToolRegistry


def _action(tool: str, params: dict, ref: str) -> LLMMessage:
    import json

    return LLMMessage(
        "assistant",
        "Thought: inspect\n"
        f"Action: {tool}\n"
        f"Params: {json.dumps(params, ensure_ascii=False)}",
        event_ref=ref,
    )


def _observation(tool: str, status: str, body: str, ref: str) -> LLMMessage:
    return LLMMessage(
        "user",
        f"[Tool: {tool} | {status}]\n{body}",
        event_ref=ref,
    )


@dataclass
class FakeSemanticSummarizer:
    fields: SemanticContextFields | None
    usage: TokenUsage = TokenUsage(input_tokens=7, output_tokens=3)
    error: str | None = None
    on_call: object = None

    def __post_init__(self):
        self.call_count = 0
        self.calls = []

    def summarize(self, *, task_description, old_messages, recent_messages, evidence):
        self.call_count += 1
        self.calls.append((task_description, tuple(old_messages), tuple(recent_messages), evidence))
        if callable(self.on_call):
            self.on_call()
        return SemanticSummaryResult(
            fields=self.fields,
            usage=self.usage,
            packet_truncated=False,
            error=self.error,
        )


def _semantic_fields() -> SemanticContextFields:
    return SemanticContextFields(
        hard_constraints=("core 最好先别碰", "Keep the public API stable"),
        decisions=("优先在 context 层实现",),
        completed=("已完成 C4 deterministic pruning",),
        in_progress=("实现 C5 Hybrid compaction",),
        blocked=(),
        next_actions=("运行定向回归测试",),
    )


def _long_history() -> ConversationHistory:
    history = ConversationHistory(max_messages=40)
    history.add(LLMMessage("user", "最初目标：完善 Context Compaction。"))
    for index in range(5):
        history.add(LLMMessage(
            "user",
            f"历史用户要求 {index}: core 最好先别碰，优先 context 层。" + ("上下文" * 500),
        ))
    history.add(_action("file_view", {"path": "context/compaction.py", "start_line": 1}, "a-recent"))
    history.add(_observation(
        "file_view",
        "SUCCESS",
        "recent working set must remain raw",
        "o-recent",
    ))
    return history


def _context(tmp_path, history, log, *, total=2_000, step=2):
    return PrepareNextTurnContext(
        task=Task("继续实现 C5", str(tmp_path), task_id="c5-test", max_steps=4),
        step=step,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=TokenBudget(total=total),
        cancel_event=None,
        event_log=log,
        system_content="system prompt",
    )


def test_shared_history_parser_extracts_stable_protocol_fields():
    action_message = _action("file_write", {"path": "context/compaction.py", "content": "x"}, "a1")
    observation_message = _observation("file_write", "SUCCESS", "Written 1 lines", "o1")

    action = parse_action_message(action_message)
    observation = parse_observation_message(observation_message)

    assert action is not None and observation is not None
    assert action.tool_name == "file_write"
    assert action.params["path"] == "context/compaction.py"
    assert observation.tool_name == "file_write"
    assert observation.status == "SUCCESS"
    assert action_fingerprint(action) == action_fingerprint(action)
    interactions = iter_interactions([action_message, observation_message])
    assert len(interactions) == 1
    assert interactions[0].observation.event_ref == "o1"


def test_deterministic_evidence_supersedes_old_test_failure_and_tracks_working_set():
    messages = [
        LLMMessage("user", "task"),
        _action("file_write", {"path": "context/compaction.py", "content": "x"}, "a-write"),
        _observation("file_write", "SUCCESS", "Written", "o-write"),
        _action("pytest", {}, "a-test-old"),
        _observation("pytest", "ERROR", "2 failed", "o-test-old"),
        _action("file_read", {"path": "agent/core.py"}, "a-read"),
        _observation("file_read", "SUCCESS", "File: agent/core.py", "o-read"),
        _action("pytest", {}, "a-test-new"),
        _observation("pytest", "SUCCESS", "12 passed", "o-test-new"),
    ]

    evidence = build_deterministic_evidence(messages, old_end_index=6)

    assert evidence.unresolved_failures == ()
    assert evidence.verification_state[0] == "PASS"
    assert "o-test-new" in " ".join(evidence.verification_state)
    assert evidence.modified_paths == ("context/compaction.py",)
    assert evidence.read_paths == ("agent/core.py",)


def test_latest_test_failure_stays_unresolved_when_it_is_in_old_region():
    messages = [
        LLMMessage("user", "task"),
        _action("pytest", {}, "a-test"),
        _observation("pytest", "ERROR", "root cause", "o-test"),
        LLMMessage("user", "continue"),
    ]

    evidence = build_deterministic_evidence(messages, old_end_index=3)

    assert evidence.verification_state[0] == "FAIL"
    assert len(evidence.unresolved_failures) == 1
    assert "o-test" in evidence.unresolved_failures[0]


def test_semantic_packet_preserves_multilingual_user_text_and_excludes_tool_observation():
    old = [
        LLMMessage("user", "core 最好先别碰，优先 context 层。"),
        _observation("shell", "ERROR", "SECRET_TOOL_OUTPUT", "o1"),
        LLMMessage("assistant", "Thought: exploratory internal reasoning"),
    ]
    recent = [LLMMessage("user", "Keep the API stable，其他可以重构。")]
    evidence = DeterministicEvidence((), ("UNKNOWN",), (), (), ())

    packet, truncated = build_semantic_packet(
        task_description="继续 C5",
        old_messages=old,
        recent_messages=recent,
        evidence=evidence,
        max_chars=10_000,
    )

    assert not truncated
    assert "core 最好先别碰" in packet
    assert "Keep the API stable" in packet
    assert "SECRET_TOOL_OUTPUT" not in packet
    assert "exploratory internal reasoning" not in packet


def test_llm_semantic_summarizer_uses_internal_tool_call_and_preserves_usage():
    params = {
        "hard_constraints": ["core 最好先别碰"],
        "decisions": ["优先 context 层"],
        "completed": [],
        "in_progress": ["C5"],
        "blocked": [],
        "next_actions": ["测试"],
        "verification_state": ["fake value that must be ignored"],
    }
    backend = MockBackend(
        [Action(ActionType.TOOL_CALL, "summary", ToolCall("record_context_summary", params))],
        input_tokens=11,
        output_tokens=5,
    )
    summarizer = LLMSemanticSummarizer(backend)

    result = summarizer.summarize(
        task_description="继续 C5",
        old_messages=[LLMMessage("user", "core 最好先别碰")],
        recent_messages=[],
        evidence=DeterministicEvidence((), ("UNKNOWN",), (), (), ()),
    )

    assert result.error is None
    assert result.fields is not None
    assert result.fields.hard_constraints == ("core 最好先别碰",)
    assert result.fields.decisions == ("优先 context 层",)
    assert not hasattr(result.fields, "verification_state")
    assert result.usage.total_tokens == 16
    assert backend.call_count == 1
    assert backend.received_messages[0][0].role == "system"


def test_malformed_semantic_response_returns_safe_error_instead_of_fields():
    backend = MockBackend([Action(ActionType.FINISH, "done", message="not a summary tool call")])
    summarizer = LLMSemanticSummarizer(backend)

    result = summarizer.summarize(
        task_description="task",
        old_messages=[LLMMessage("user", "要求")],
        recent_messages=[],
        evidence=DeterministicEvidence((), ("UNKNOWN",), (), (), ()),
    )

    assert result.fields is None
    assert result.error is not None
    assert "tool call" in result.error


def test_structured_renderer_keeps_all_sections_under_bounded_budget():
    state = build_structured_state(
        goal="实现 C5 Hybrid compaction",
        semantic=_semantic_fields(),
        evidence=DeterministicEvidence(
            unresolved_failures=("shell ERROR: example",),
            verification_state=("PASS", "12 passed", "historical only"),
            read_paths=("agent/core.py",),
            modified_paths=("context/compaction.py",),
            historical_references=tuple(f"ref-{i}" for i in range(30)),
        ),
    )

    rendered = render_structured_context(state, max_chars=2_000)

    assert len(rendered) <= 2_000
    for heading in (
        "## Goal",
        "## Hard Constraints",
        "## Decisions",
        "## Progress",
        "## Unresolved Failures",
        "## Verification State",
        "## Working Set",
        "## Next Actions",
        "## Historical References",
    ):
        assert heading in rendered


def test_hybrid_stage_b_emits_started_before_summarizer_and_preserves_canonical(tmp_path):
    history = _long_history()
    canonical_before = history.to_dicts()
    task = Task("继续实现 C5", str(tmp_path), task_id="hybrid", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

    def assert_started_already_logged():
        types = [event.event_type for event in log.replay()]
        assert EventType.CONTEXT_COMPACTION_STARTED in types
        assert EventType.CONTEXT_COMPACTED not in types

    fake = FakeSemanticSummarizer(_semantic_fields(), on_call=assert_started_already_logged)
    strategy = TraceableCompaction(
        threshold=0.6,
        target_ratio=0.3,
        keep_recent_tokens=80,
        max_summary_chars=1_600,
        semantic_summarizer=fake,
    )

    result = strategy(_context(tmp_path, history, log, total=2_000))

    assert result is not None and result.history_override is not None
    assert fake.call_count == 1
    assert history.to_dicts() == canonical_before
    visible = "\n".join(message.content for message in result.history_override)
    assert "## Hard Constraints" in visible
    assert "core 最好先别碰" in visible
    assert "recent working set must remain raw" in visible
    checkpoint = strategy.checkpoints[0]
    assert checkpoint.summary_method == "structured-hybrid-v1"
    assert checkpoint.summary_usage["total_tokens"] == 10
    event_types = [event.event_type for event in log.replay()]
    assert event_types.index(EventType.CONTEXT_COMPACTION_STARTED) < event_types.index(EventType.CONTEXT_COMPACTED)
    log.close()


def test_active_compacted_view_reuses_summary_without_recalling_semantic_model(tmp_path):
    history = _long_history()
    task = Task("继续实现 C5", str(tmp_path), task_id="active", max_steps=3)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    fake = FakeSemanticSummarizer(_semantic_fields())
    strategy = TraceableCompaction(
        threshold=0.6,
        target_ratio=0.3,
        keep_recent_tokens=80,
        max_summary_chars=1_400,
        semantic_summarizer=fake,
    )
    context = _context(tmp_path, history, log, total=2_000)

    first = strategy(context)
    second = strategy(context)

    assert first is not None and second is not None
    assert fake.call_count == 1
    assert len(strategy.entries) == 1
    log.close()


def test_semantic_failure_records_failed_event_and_uses_structured_fallback(tmp_path):
    history = _long_history()
    task = Task("继续实现 C5", str(tmp_path), task_id="fallback", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    fake = FakeSemanticSummarizer(None, error="malformed semantic tool call")
    strategy = TraceableCompaction(
        threshold=0.6,
        target_ratio=0.3,
        keep_recent_tokens=80,
        max_summary_chars=1_600,
        semantic_summarizer=fake,
    )

    result = strategy(_context(tmp_path, history, log, total=2_000))

    assert result is not None and result.history_override is not None
    assert strategy.checkpoints[0].summary_method == "structured-fallback-v1"
    visible = "\n".join(message.content for message in result.history_override)
    assert "Unclassified user excerpt" in visible
    event_types = [event.event_type for event in log.replay()]
    assert EventType.CONTEXT_COMPACTION_STARTED in event_types
    assert EventType.CONTEXT_COMPACTION_FAILED in event_types
    assert EventType.CONTEXT_COMPACTED in event_types
    log.close()


def test_chat_live_printer_surfaces_context_compaction_marker(capsys):
    _print_event_live(Event(
        event_type=EventType.CONTEXT_COMPACTION_STARTED,
        task_id="task",
        payload={"step": 2},
    ))

    assert "[压缩上下文]" in capsys.readouterr().out


def test_chat_merges_semantic_summary_usage_into_round_and_session_usage(tmp_path):
    config = AppConfig()
    config.agent.max_steps = 1
    config.agent.budget_tokens = 2_000
    config.agent.log_dir = str(tmp_path / "logs")
    config.context.history_window = 20
    fake = FakeSemanticSummarizer(_semantic_fields())
    strategy = TraceableCompaction(
        threshold=0.6,
        target_ratio=0.3,
        keep_recent_tokens=80,
        max_summary_chars=1_400,
        semantic_summarizer=fake,
    )
    backend = MockBackend(
        [Action(ActionType.FINISH, "done", message="Done.")],
        input_tokens=100,
        output_tokens=50,
    )
    session = ChatSession(
        backend=backend,
        registry=ToolRegistry().register(NoopTool("noop")),
        config=config,
        repo_path=str(tmp_path),
        log_dir=config.agent.log_dir,
        prepare_next_turn=strategy,
        stream=False,
    )
    for index in range(5):
        session._shared_history.add(LLMMessage(
            "user",
            f"历史用户要求 {index}: core 最好先别碰。" + ("上下文" * 500),
        ))

    assert session.run_round("继续 C5")

    assert fake.call_count == 1
    assert session.usage.llm_calls == 2
    assert session.usage.total_tokens == 160
    assert session.total_tokens == 160
