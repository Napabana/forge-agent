from __future__ import annotations

import json

import pytest

from agent.task import Action, ActionType
from config.schema import load_config
from context.structured_compaction import DeterministicEvidence, build_semantic_packet
from context.token_budget import ConservativeTokenCounter, TokenBudget
from llm.base import LLMMessage, MockBackend
from llm.router import _attach_capabilities


class _CharCounter:
    """Deterministic local counter for budget-formula tests."""

    name = "test-char-counter"
    estimated = True

    def count_text(self, text: str) -> int:
        return len(text)

    def count_message(self, message: dict) -> int:
        return 4 + len(str(message.get("role", ""))) + len(str(message.get("content", "")))

    def count_messages(self, messages: list[dict]) -> int:
        return sum(self.count_message(message) for message in messages)

    def count_tool_schemas(self, tools) -> int:
        payload = [
            {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "parameters": getattr(tool, "parameters", {}),
            }
            for tool in tools
        ]
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) if payload else 0


def _budget(window: int, *, cap: int | None = None) -> TokenBudget:
    return TokenBudget(
        model_context_window=window,
        model_max_output_tokens=16_384,
        request_max_output_tokens=4_096,
        context_budget_cap=cap,
        safety_margin_tokens=1_024,
        token_counter=_CharCounter(),
    )


def test_same_request_has_different_pressure_for_32k_and_128k_models() -> None:
    history = [{"role": "user", "content": "x" * 27_000}]

    small = _budget(32_000).request_pressure(
        system_text="system",
        repo_map_text="",
        history=history,
    )
    large = _budget(128_000).request_pressure(
        system_text="system",
        repo_map_text="",
        history=history,
    )

    assert small.ratio > 0.8
    assert large.ratio < 0.8
    assert small.projected_input == large.projected_input


def test_output_reserve_uses_request_max_not_fixed_percentage() -> None:
    budget = TokenBudget(
        model_context_window=128_000,
        model_max_output_tokens=32_768,
        request_max_output_tokens=8_192,
        safety_margin_tokens=1_024,
        token_counter=_CharCounter(),
    )
    plan = budget.default_plan()

    assert plan.reserved_output_tokens == 8_192
    assert plan.safety_margin_tokens == 1_024
    assert plan.reserve == 9_216
    assert plan.available == 128_000 - 8_192 - 1_024
    assert plan.reserve != int(128_000 * 0.15)


def test_context_cap_limits_effective_model_window() -> None:
    budget = _budget(128_000, cap=80_000)

    assert budget.effective_context_window == 80_000
    assert budget.available_input_tokens == 80_000 - 4_096 - 1_024


def test_semantic_packet_is_token_bounded_for_mixed_history() -> None:
    counter = ConservativeTokenCounter()
    old = [
        LLMMessage("user", "中文约束" * 120),
        LLMMessage("user", "English context " * 120),
        LLMMessage("user", "```python\ndef f(x): return x + 1\n```" * 80),
    ]
    evidence = DeterministicEvidence((), ("UNKNOWN",), (), (), ())

    packet, truncated = build_semantic_packet(
        task_description="继续实现 token-aware compaction",
        old_messages=old,
        recent_messages=[],
        evidence=evidence,
        max_tokens=420,
        token_counter=counter,
    )

    assert truncated
    assert counter.count_text(packet) <= 420


def test_semantic_packet_prefers_recent_user_instruction_under_pressure() -> None:
    counter = ConservativeTokenCounter()
    old = [
        LLMMessage(
            "user",
            "OLD_INSTRUCTION 不要修改 core.py。" + (" very old evidence" * 300),
        ),
        LLMMessage("user", "NEW_OVERRIDE 现在可以修改 core.py。"),
    ]
    evidence = DeterministicEvidence((), ("UNKNOWN",), (), (), ())

    packet, truncated = build_semantic_packet(
        task_description="继续收口",
        old_messages=old,
        recent_messages=[],
        evidence=evidence,
        max_tokens=300,
        token_counter=counter,
    )

    assert truncated
    assert "NEW_OVERRIDE" in packet
    assert "OLD_INSTRUCTION" not in packet
    assert counter.count_text(packet) <= 300


def test_pre_request_estimate_does_not_overwrite_provider_reported_usage() -> None:
    budget = _budget(32_000)
    pressure = budget.request_pressure(
        system_text="system",
        repo_map_text="",
        history=[{"role": "user", "content": "estimated request"}],
    )
    backend = MockBackend(
        [Action(ActionType.FINISH, "done", message="done")],
        input_tokens=321,
        output_tokens=45,
        estimated=False,
    )

    response = backend.complete([], [])

    assert pressure.projected_input != response.usage.input_tokens
    assert response.usage.input_tokens == 321
    assert response.usage.output_tokens == 45
    assert response.usage.estimated is False


def test_legacy_config_fields_map_to_request_limit_and_context_cap(tmp_path) -> None:
    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  model: unknown-proxy-model
  api_key: dummy
  max_tokens: 8192
agent:
  budget_tokens: 80000
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.llm.max_output_tokens == 8_192
    assert config.agent.context_budget_cap == 80_000
    with pytest.warns(RuntimeWarning, match="context_window is unknown"):
        budget = TokenBudget(total=config.agent.budget_tokens)
    assert budget.effective_context_window == 80_000
    assert budget.reserved_output_tokens == 8_192
    assert budget.safety_margin_tokens == 1_024


def test_backend_exposes_capability_and_request_policy_metadata() -> None:
    backend = MockBackend([])

    _attach_capabilities(
        backend,
        context_window=128_000,
        model_max_output_tokens=32_768,
        request_max_output_tokens=8_192,
        semantic_packet_max_tokens=16_000,
        context_budget_cap=80_000,
        context_safety_margin_tokens=1_024,
    )

    assert backend.model_capabilities.context_window == 128_000
    assert backend.model_capabilities.max_output_tokens == 32_768
    assert backend.context_window == 128_000
    assert backend.model_max_output_tokens == 32_768
    assert backend.request_max_output_tokens == 8_192
    assert backend.context_budget_cap == 80_000
