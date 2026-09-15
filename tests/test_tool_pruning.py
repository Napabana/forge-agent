from agent.core import PrepareNextTurnContext
from agent.event_log import EventLog
from agent.task import Task
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import (
    TokenBudget,
    history_unit_tokens,
    history_units,
    recent_history_units,
)
from context.tool_pruning import DeterministicToolPruner
from llm.base import LLMMessage


def _action(tool: str, params: str, ref: str) -> LLMMessage:
    return LLMMessage(
        "assistant",
        f"Thought: inspect\nAction: {tool}\nParams: {params}",
        event_ref=ref,
    )


def _observation(tool: str, status: str, body: str, ref: str) -> LLMMessage:
    return LLMMessage(
        "user",
        f"[Tool: {tool} | {status}]\n{body}",
        event_ref=ref,
    )


def test_large_old_file_read_is_pruned_but_recent_file_read_is_preserved():
    old_body = "File: src/old.py (400 lines total)\n" + ("old source line\n" * 400)
    recent_body = "File: src/recent.py (400 lines total)\n" + ("recent source line\n" * 400)
    messages = [
        LLMMessage("user", "hard constraint: do not edit config"),
        _action("file_read", '{"path":"src/old.py"}', "a-old"),
        _observation("file_read", "SUCCESS", old_body, "o-old"),
        _action("file_read", '{"path":"src/recent.py"}', "a-recent"),
        _observation("file_read", "SUCCESS", recent_body, "o-recent"),
    ]

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=3,
    )

    assert result.pruned_event_ids == ("o-old",)
    assert result.pruned_units == 1
    assert result.after_tokens < result.before_tokens
    assert "File: src/old.py (400 lines total)" in result.messages[2].content
    assert "old source line" not in result.messages[2].content
    assert "full_output_available=true" in result.messages[2].content
    assert result.messages[2].event_ref == "o-old"
    assert result.messages[4].content == messages[4].content
    assert result.messages[0].content == messages[0].content


def test_error_observation_is_never_pruned():
    body = "traceback\n" + ("root cause evidence\n" * 400)
    messages = [
        LLMMessage("user", "task"),
        _action("shell", '{"cmd":"pytest"}', "a1"),
        _observation("shell", "ERROR", body, "o1"),
    ]

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=3,
    )

    assert result.pruned_units == 0
    assert result.messages[2].content == messages[2].content


def test_large_shell_success_keeps_head_and_tail_while_small_shell_is_unchanged():
    large_body = "HEAD_MARK\n" + ("x" * 2_000) + "\nTAIL_MARK"
    small_body = "short output"
    messages = [
        LLMMessage("user", "task"),
        _action("shell", '{"cmd":"long-command"}', "a1"),
        _observation("shell", "SUCCESS", large_body, "o1"),
        _action("shell", '{"cmd":"pwd"}', "a2"),
        _observation("shell", "SUCCESS", small_body, "o2"),
    ]

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=5,
    )

    assert result.pruned_event_ids == ("o1",)
    assert "HEAD_MARK" in result.messages[2].content
    assert "TAIL_MARK" in result.messages[2].content
    assert "[pruned" in result.messages[2].content
    assert result.messages[4].content == messages[4].content


def test_large_test_success_keeps_tail_and_failed_test_remains_raw():
    success_body = "setup noise\n" + ("noise\n" * 300) + "12 passed in 0.42s"
    failed_body = "failure evidence\n" + ("trace line\n" * 300)
    messages = [
        LLMMessage("user", "task"),
        _action("test", "{}", "a1"),
        _observation("test", "SUCCESS", success_body, "o1"),
        _action("test", "{}", "a2"),
        _observation("test", "ERROR", failed_body, "o2"),
    ]

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=5,
    )

    assert result.pruned_event_ids == ("o1",)
    assert "12 passed in 0.42s" in result.messages[2].content
    assert "setup noise" not in result.messages[2].content
    assert result.messages[4].content == messages[4].content


def test_exact_duplicate_search_prunes_only_older_interaction():
    action = _action("search_text", '{"pattern":"needle"}', "a-old")
    output = "src/a.py:10: needle\n[Showing 1 matches]"
    messages = [
        LLMMessage("user", "task"),
        action,
        _observation("search_text", "SUCCESS", output, "o-old"),
        _action("search_text", '{"pattern":"needle"}', "a-new"),
        _observation("search_text", "SUCCESS", output, "o-new"),
    ]
    # exact interaction 要求 Action content 也相同；event_ref 不参与比较。
    messages[3].content = action.content

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=3,
    )

    assert result.pruned_event_ids == ("o-old",)
    assert "[Pruned duplicate tool output]" in result.messages[2].content
    assert "duplicate_of_event_ref=o-new" in result.messages[2].content
    assert result.messages[4].content == messages[4].content


def test_different_search_query_or_output_is_not_deduplicated():
    messages = [
        LLMMessage("user", "task"),
        _action("search_text", '{"pattern":"alpha"}', "a1"),
        _observation("search_text", "SUCCESS", "a.py:1: alpha", "o1"),
        _action("search_text", '{"pattern":"beta"}', "a2"),
        _observation("search_text", "SUCCESS", "b.py:2: beta", "o2"),
    ]

    result = DeterministicToolPruner(min_output_tokens=20).prune(
        messages,
        protected_from_index=5,
    )

    assert result.pruned_units == 0
    assert result.messages == tuple(messages)


def test_pruning_is_deterministic_and_keeps_action_observation_count():
    body = "File: src/a.py (300 lines total)\n" + ("line\n" * 300)
    messages = [
        LLMMessage("user", "task"),
        _action("file_read", '{"path":"src/a.py"}', "a1"),
        _observation("file_read", "SUCCESS", body, "o1"),
    ]
    pruner = DeterministicToolPruner(min_output_tokens=20)

    first = pruner.prune(messages, protected_from_index=3)
    second = pruner.prune(messages, protected_from_index=3)

    assert first == second
    assert len(first.messages) == len(messages)
    assert first.messages[1].content == messages[1].content
    assert first.messages[2].event_ref == messages[2].event_ref


def _history_for_compaction(*, include_unprunable_old_output: bool = False) -> ConversationHistory:
    history = ConversationHistory(max_messages=40)
    history.add(LLMMessage("user", "hard constraint: preserve public API"))
    large_file = (
        "File: src/large.py (900 lines total)\n"
        + ("SECRET_TOOL_BODY_MARKER source line with details\n" * 900)
    )
    history.add(_action("file_read", '{"path":"src/large.py"}', "a-file"))
    history.add(_observation("file_read", "SUCCESS", large_file, "o-file"))
    if include_unprunable_old_output:
        history.add(LLMMessage(
            "assistant",
            "Thought: preserve unresolved context\nAction: custom_tool\nParams: {}",
            event_ref="a-custom",
        ))
        history.add(LLMMessage(
            "user",
            "[Tool: custom_tool | SUCCESS]\n" + ("important unprunable state line\n" * 900),
            event_ref="o-custom",
        ))
    history.add(_action("file_view", '{"path":"src/current.py","start_line":1}', "a-recent"))
    history.add(_observation(
        "file_view",
        "SUCCESS",
        "recent working set must remain raw",
        "o-recent",
    ))
    return history


def _recent_budget(history: ConversationHistory) -> int:
    raw = history.to_dicts()
    units = history_units(raw)
    return sum(history_unit_tokens(raw, unit) for unit in units[-1:])


def _context(tmp_path, history: ConversationHistory, budget: TokenBudget, log: EventLog):
    task = Task("continue", str(tmp_path), task_id="pruning-integration", max_steps=2)
    return PrepareNextTurnContext(
        task=task,
        step=2,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=budget,
        cancel_event=None,
        event_log=log,
        system_content="system prompt",
    )


def test_compaction_returns_pruning_only_view_when_stage_a_relief_is_enough(tmp_path):
    history = _history_for_compaction()
    canonical_before = history.to_dicts()
    keep_recent_tokens = _recent_budget(history)
    pruner = DeterministicToolPruner(min_output_tokens=20)
    raw = history.to_dicts()
    recent = recent_history_units(raw, keep_recent_tokens)
    tail_start = recent[0].indices[0]
    pruned = pruner.prune(history.to_list(), protected_from_index=tail_start)

    probe = TokenBudget(total=50_000)
    before_projected = probe.request_pressure(
        system_text="system prompt", repo_map_text="", history=raw,
    ).projected_input
    after_projected = probe.request_pressure(
        system_text="system prompt",
        repo_map_text="",
        history=[{"role": m.role, "content": m.content} for m in pruned.messages],
    ).projected_input
    total = max(500, int(before_projected / (0.80 * 0.85)))
    budget = TokenBudget(total=total)
    before_ratio = budget.request_pressure(
        system_text="system prompt", repo_map_text="", history=raw,
    ).ratio
    after_ratio = budget.request_pressure(
        system_text="system prompt",
        repo_map_text="",
        history=[{"role": m.role, "content": m.content} for m in pruned.messages],
    ).ratio
    assert after_ratio < before_ratio
    threshold = (before_ratio + after_ratio) / 2

    strategy = TraceableCompaction(
        threshold=threshold,
        target_ratio=threshold / 2,
        keep_recent_tokens=keep_recent_tokens,
        pruner=pruner,
    )
    task = Task("continue", str(tmp_path), task_id="pruning-only", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    context = PrepareNextTurnContext(
        task=task,
        step=2,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=budget,
        cancel_event=None,
        event_log=log,
        system_content="system prompt",
    )

    result = strategy(context)

    assert result is not None and result.history_override is not None
    visible = "\n".join(message.content for message in result.history_override)
    assert "[Pruned old tool output]" in visible
    assert "[Compacted earlier context" not in visible
    assert "recent working set must remain raw" in visible
    assert history.to_dicts() == canonical_before
    assert strategy.entries == []
    checkpoint = strategy.checkpoints[0]
    assert checkpoint.summary_method == "none"
    assert checkpoint.pruning_method == pruner.method
    assert checkpoint.pruned_event_ids == ("o-file",)
    assert checkpoint.pruned_after_tokens < checkpoint.pruned_before_tokens
    log.close()


def test_stage_b_fallback_does_not_reintroduce_pruned_old_tool_body(tmp_path):
    from context.structured_compaction import SemanticSummaryResult
    from llm.usage import TokenUsage

    class FailingSemanticSummarizer:
        def summarize(self, **kwargs):
            return SemanticSummaryResult(
                fields=None,
                usage=TokenUsage(input_tokens=3, output_tokens=1),
                packet_truncated=False,
                error="forced semantic failure",
            )

    history = _history_for_compaction(include_unprunable_old_output=True)
    canonical_before = history.to_dicts()
    keep_recent_tokens = _recent_budget(history)
    pruner = DeterministicToolPruner(min_output_tokens=20)
    raw = history.to_dicts()
    recent = recent_history_units(raw, keep_recent_tokens)
    tail_start = recent[0].indices[0]
    pruned = pruner.prune(history.to_list(), protected_from_index=tail_start)
    probe = TokenBudget(total=100_000)
    pruned_projected = probe.request_pressure(
        system_text="system prompt",
        repo_map_text="",
        history=[{"role": m.role, "content": m.content} for m in pruned.messages],
    ).projected_input
    total = max(500, int(pruned_projected / (0.80 * 0.85)))
    budget = TokenBudget(total=total)
    pruned_ratio = budget.request_pressure(
        system_text="system prompt",
        repo_map_text="",
        history=[{"role": m.role, "content": m.content} for m in pruned.messages],
    ).ratio
    threshold = min(0.95, pruned_ratio * 0.9)
    assert 0 < threshold < pruned_ratio

    strategy = TraceableCompaction(
        threshold=threshold,
        target_ratio=threshold / 2,
        keep_recent_tokens=keep_recent_tokens,
        max_summary_chars=4_000,
        pruner=pruner,
        semantic_summarizer=FailingSemanticSummarizer(),
    )
    task = Task("continue", str(tmp_path), task_id="pruning-stage-b", max_steps=2)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    context = PrepareNextTurnContext(
        task=task,
        step=2,
        history=history,
        repo_map=RepoMap(tmp_path),
        token_budget=budget,
        cancel_event=None,
        event_log=log,
        system_content="system prompt",
    )

    result = strategy(context)

    assert result is not None and result.history_override is not None
    visible = "\n".join(message.content for message in result.history_override)
    assert "[Compacted earlier context" in visible
    assert "SECRET_TOOL_BODY_MARKER" not in visible
    assert "## Working Set" in strategy.entries[0].summary_text
    assert history.to_dicts() == canonical_before
    checkpoint = strategy.checkpoints[0]
    assert checkpoint.summary_method == "structured-fallback-v1"
    assert checkpoint.pruning_method == pruner.method
    assert checkpoint.pruned_event_ids == ("o-file",)
    assert checkpoint.pruned_after_tokens < checkpoint.pruned_before_tokens
    log.close()
