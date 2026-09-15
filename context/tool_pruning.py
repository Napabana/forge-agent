"""Deterministic pruning for old tool outputs in the model-visible history copy."""

from __future__ import annotations

from dataclasses import dataclass

from context.history_evidence import (
    ParsedObservation,
    parse_action_message,
    parse_observation_message,
)
from context.token_budget import estimate_messages_tokens, estimate_tokens, history_units
from llm.base import LLMMessage


_SEARCH_TOOLS = frozenset({"search_text", "find_files", "find_symbol"})
_LARGE_OUTPUT_TOKENS = 200
_SHELL_PREVIEW_CHARS = 320
_TEST_TAIL_LINES = 4
_TEST_TAIL_CHARS = 800


@dataclass(frozen=True)
class PruningResult:
    """一次 Stage A pruning 的可审计结果。"""

    messages: tuple[LLMMessage, ...]
    pruned_event_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    pruned_units: int


class DeterministicToolPruner:
    """只裁剪旧的、可确定识别的 SUCCESS Tool Observation。"""

    method = "deterministic-tool-pruning-v1"

    def __init__(self, *, min_output_tokens: int = _LARGE_OUTPUT_TOKENS) -> None:
        if min_output_tokens <= 0:
            raise ValueError("min_output_tokens must be positive")
        self.min_output_tokens = min_output_tokens

    def prune(
        self,
        messages: list[LLMMessage],
        *,
        protected_from_index: int,
    ) -> PruningResult:
        """返回 model-visible copy；`protected_from_index` 之后完全保持原文。"""
        working = list(messages)
        before_tokens = _messages_tokens(working)
        replacements: dict[int, LLMMessage] = {}

        units = history_units(_message_dicts(messages))
        latest_duplicate: dict[tuple[str, str, str], str] = {}

        # 从新到旧扫描，让最新 exact duplicate 保持原文，旧副本始终指向它。
        for unit in reversed(units):
            if len(unit.indices) != 2:
                continue
            action_index, observation_index = unit.indices
            action_message = messages[action_index]
            observation_message = messages[observation_index]
            parsed_action = parse_action_message(action_message)
            parsed_observation = parse_observation_message(observation_message)
            if (
                parsed_action is None
                or parsed_observation is None
                or parsed_observation.status != "SUCCESS"
                or parsed_action.tool_name != parsed_observation.tool_name
            ):
                continue

            duplicate_key = (
                action_message.content,
                parsed_observation.tool_name,
                observation_message.content,
            )
            if parsed_observation.tool_name in _SEARCH_TOOLS and observation_message.event_ref:
                newer_ref = latest_duplicate.get(duplicate_key)
                if newer_ref is None:
                    latest_duplicate[duplicate_key] = observation_message.event_ref
                elif observation_index < protected_from_index:
                    replacements[observation_index] = _copy_message(
                        observation_message,
                        _duplicate_marker(parsed_observation.tool_name, newer_ref),
                    )
                    continue

            if observation_index >= protected_from_index or not observation_message.event_ref:
                continue

            replacement = self._prune_large_success(
                observation_message,
                parsed_observation,
            )
            if replacement is not None:
                replacements[observation_index] = replacement

        for index, replacement in replacements.items():
            working[index] = replacement

        pruned_event_ids = tuple(
            messages[index].event_ref
            for index in sorted(replacements)
            if messages[index].event_ref is not None
        )
        return PruningResult(
            messages=tuple(working),
            pruned_event_ids=pruned_event_ids,
            before_tokens=before_tokens,
            after_tokens=_messages_tokens(working),
            pruned_units=len(replacements),
        )

    def _prune_large_success(
        self,
        message: LLMMessage,
        parsed: ParsedObservation,
    ) -> LLMMessage | None:
        body_tokens = estimate_tokens(parsed.body)
        if body_tokens < self.min_output_tokens:
            return None

        if parsed.tool_name == "file_read":
            content = _file_read_marker(message, parsed, body_tokens)
        elif parsed.tool_name == "shell":
            content = _shell_marker(message, parsed, body_tokens)
        elif parsed.tool_name in {"test", "pytest"}:
            content = _test_marker(message, parsed, body_tokens)
        else:
            return None
        return _copy_message(message, content)


def _copy_message(message: LLMMessage, content: str) -> LLMMessage:
    return LLMMessage(
        role=message.role,
        content=content,
        tool_call_id=message.tool_call_id,
        event_ref=message.event_ref,
    )


def _audit_marker(message: LLMMessage, body: str, body_tokens: int) -> str:
    return (
        f"{body}\n"
        "[Pruned old tool output]\n"
        f"original_chars={len(message.content)}\n"
        f"original_body_tokens={body_tokens}\n"
        f"event_ref={message.event_ref}\n"
        "full_output_available=true"
    )


def _file_read_marker(
    message: LLMMessage,
    parsed: ParsedObservation,
    body_tokens: int,
) -> str:
    metadata = next(
        (line for line in parsed.body.splitlines() if line.startswith("File: ")),
        "File metadata unavailable",
    )
    body = f"[Tool: {parsed.tool_name} | SUCCESS]\n{metadata}"
    return _audit_marker(message, body, body_tokens)


def _shell_marker(
    message: LLMMessage,
    parsed: ParsedObservation,
    body_tokens: int,
) -> str:
    body = parsed.body
    if len(body) <= _SHELL_PREVIEW_CHARS * 2:
        preview = body
    else:
        omitted = len(body) - (_SHELL_PREVIEW_CHARS * 2)
        preview = (
            body[:_SHELL_PREVIEW_CHARS]
            + f"\n... [pruned {omitted} chars] ...\n"
            + body[-_SHELL_PREVIEW_CHARS:]
        )
    header = f"[Tool: {parsed.tool_name} | SUCCESS]\n{preview}".rstrip()
    return _audit_marker(message, header, body_tokens)


def _test_marker(
    message: LLMMessage,
    parsed: ParsedObservation,
    body_tokens: int,
) -> str:
    lines = [line for line in parsed.body.splitlines() if line.strip()]
    tail = "\n".join(lines[-_TEST_TAIL_LINES:])[-_TEST_TAIL_CHARS:]
    header = f"[Tool: {parsed.tool_name} | SUCCESS]\n{tail}".rstrip()
    return _audit_marker(message, header, body_tokens)


def _duplicate_marker(tool_name: str, newer_event_ref: str) -> str:
    return (
        f"[Tool: {tool_name} | SUCCESS]\n"
        "[Pruned duplicate tool output]\n"
        f"duplicate_of_event_ref={newer_event_ref}\n"
        "full_output_available=true"
    )


def _message_dicts(messages: list[LLMMessage]) -> list[dict]:
    return [
        {
            "role": message.role,
            "content": message.content,
            **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
        }
        for message in messages
    ]


def _messages_tokens(messages: list[LLMMessage]) -> int:
    return estimate_messages_tokens(_message_dicts(messages))
