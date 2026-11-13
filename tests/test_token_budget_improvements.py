from __future__ import annotations

from context import token_budget as token_module
from context.token_budget import (
    TokenBudget,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
)


def test_trim_to_binary_searches_aggregate_body_and_notice(monkeypatch):
    monkeypatch.setattr(token_module, "estimate_tokens", len)
    result = TokenBudget().trim_to("x" * 100, token_limit=30)

    assert result.endswith("... [tokens truncated]")
    assert len(result) == 30


def test_trim_to_never_exceeds_active_estimator():
    text = "中英文 mixed tokens " * 500
    result = TokenBudget().trim_to(text, token_limit=73)

    assert estimate_tokens(result) <= 73


def test_message_estimate_includes_role_and_protocol_overhead():
    message = {"role": "user", "content": "hello"}

    assert estimate_message_tokens(message) > estimate_tokens("hello")


def test_history_keeps_action_observation_units_atomic():
    budget = TokenBudget()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old action"},
        {"role": "user", "content": "old observation"},
        {"role": "assistant", "content": "very large action " + "x" * 5000},
        {"role": "user", "content": "very large observation " + "x" * 5000},
        {"role": "assistant", "content": "recent action"},
        {"role": "user", "content": "recent observation"},
    ]
    intended = [
        messages[0], messages[1], messages[2], budget._history_notice(2),
        messages[5], messages[6],
    ]

    result = budget.trim_history(messages, estimate_messages_tokens(intended))
    contents = [message["content"] for message in result]

    assert "old action" in contents and "old observation" in contents
    assert "recent action" in contents and "recent observation" in contents
    assert not any("very large" in content for content in contents)
    assert estimate_messages_tokens(result) <= estimate_messages_tokens(intended)


def test_history_result_is_strictly_within_protocol_budget():
    budget = TokenBudget()
    messages = [{"role": "user", "content": "task"}]
    messages.extend(
        {"role": "user", "content": f"message {index} " + "x" * 80}
        for index in range(12)
    )

    result = budget.trim_history(messages, token_limit=80)

    assert estimate_messages_tokens(result) <= 80