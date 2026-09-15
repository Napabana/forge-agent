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
