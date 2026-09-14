"""OpenAI Responses API backend for ``POST /v1/responses``."""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.task import Action, ActionType, ToolCall
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema, StreamCallback
from llm.usage import TokenUsage

logger = logging.getLogger(__name__)


class OpenAIResponsesBackend(LLMBackend):
    """Call an OpenAI-compatible Responses API endpoint."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_function_calling(self) -> bool:
        return True

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        input_items = _to_responses_input(messages)
        kwargs = self._request_kwargs(input_items, tools)
        logger.debug(
            "Responses request: model=%s messages=%d tools=%d",
            self._model, len(input_items), len(tools),
        )
        response = self._client.responses.create(**kwargs)
        return _parse_response(response, input_items)

    def stream(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        on_text: StreamCallback | None = None,
        on_thought: StreamCallback | None = None,
    ) -> LLMResponse:
        input_items = _to_responses_input(messages)
        kwargs = self._request_kwargs(input_items, tools)
        kwargs["stream"] = True

        full_text = ""
        full_reasoning = ""
        final_response = None
        output_items: dict[int, Any] = {}
        argument_deltas: dict[str, str] = {}

        stream = self._client.responses.create(**kwargs)
        for event in stream:
            event_type = _field(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = _string_value(_field(event, "delta", ""))
                if delta:
                    full_text += delta
                    if on_text:
                        on_text(delta)
                continue

            if event_type in {
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            }:
                delta = _string_value(_field(event, "delta", ""))
                if delta:
                    full_reasoning += delta
                    if on_thought:
                        on_thought(delta)
                continue

            if event_type == "response.function_call_arguments.delta":
                item_id = _string_value(_field(event, "item_id", ""))
                if item_id:
                    argument_deltas[item_id] = (
                        argument_deltas.get(item_id, "")
                        + _string_value(_field(event, "delta", ""))
                    )
                continue

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
            }:
                index = _field(event, "output_index", len(output_items))
                if not isinstance(index, int):
                    index = len(output_items)
                item = _field(event, "item", None)
                if item is not None:
                    output_items[index] = item
                continue

            if event_type in {
                "response.completed",
                "response.failed",
                "response.incomplete",
            }:
                final_response = _field(event, "response", None)

        if final_response is not None:
            parsed = _parse_response(final_response, input_items)
            if full_reasoning and parsed.action.action_type == ActionType.FINISH:
                parsed.action.thought = full_reasoning
            return parsed

        return _parse_stream_fallback(
            output_items=output_items,
            argument_deltas=argument_deltas,
            full_text=full_text,
            full_reasoning=full_reasoning,
            input_items=input_items,
        )

    def _request_kwargs(
        self,
        input_items: list[dict[str, Any]],
        tools: list[LLMToolSchema],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = [_to_responses_tool(tool) for tool in tools]
            kwargs["tool_choice"] = "auto"
        return kwargs


def _to_responses_input(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.tool_call_id:
            result.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            })
            continue
        role = message.role
        if role not in {"system", "developer", "user", "assistant"}:
            role = "user"
        result.append({"role": role, "content": message.content})
    return result


def _to_responses_tool(schema: LLMToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "name": schema.name,
        "description": schema.description,
        "parameters": schema.parameters,
        "strict": False,
    }


def _parse_response(response: Any, input_items: list[dict[str, Any]]) -> LLMResponse:
    output = _field(response, "output", None) or []
    function_call = next(
        (item for item in output if _field(item, "type", "") == "function_call"),
        None,
    )
    text = _response_output_text(response, output)
    reasoning = _response_reasoning_text(output)
    usage = _response_usage(response, input_items, text)

    if function_call is not None:
        action = _function_call_action(function_call, reasoning or text)
    elif text:
        action = Action(ActionType.FINISH, reasoning, message=text)
    else:
        error = _field(response, "error", None)
        error_message = _string_value(_field(error, "message", ""))
        status = _string_value(_field(response, "status", ""))
        reason = error_message or (
            f"Responses API returned no usable output (status={status or 'unknown'})"
        )
        action = Action(ActionType.GIVE_UP, reasoning, message=reason)

    return LLMResponse(
        action=action,
        raw_content=text,
        usage=usage,
    )


def _function_call_action(item: Any, thought: str) -> Action:
    arguments = _field(item, "arguments", "") or "{}"
    try:
        params = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        params = {"raw": arguments}
    if not isinstance(params, dict):
        params = {"value": params}
    name = _string_value(_field(item, "name", "")) or "unknown_tool"
    return Action(
        ActionType.TOOL_CALL,
        thought,
        tool_call=ToolCall(name=name, params=params),
    )


def _response_output_text(response: Any, output: list[Any]) -> str:
    text = _field(response, "output_text", "")
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in output:
        if _field(item, "type", "") != "message":
            continue
        for content in _field(item, "content", None) or []:
            if _field(content, "type", "") == "output_text":
                value = _string_value(_field(content, "text", ""))
                if value:
                    parts.append(value)
    return "".join(parts)


def _response_reasoning_text(output: list[Any]) -> str:
    parts: list[str] = []
    for item in output:
        if _field(item, "type", "") != "reasoning":
            continue
        for key in ("summary", "content"):
            for part in _field(item, key, None) or []:
                value = _string_value(
                    _field(part, "text", None) or _field(part, "summary", None)
                )
                if value:
                    parts.append(value)
    return "".join(parts)


def _response_usage(
    response: Any,
    input_items: list[dict[str, Any]],
    output_text: str,
) -> TokenUsage:
    raw_usage = _field(response, "usage", None)
    input_tokens = _field(raw_usage, "input_tokens", None)
    output_tokens = _field(raw_usage, "output_tokens", None)
    input_details = _field(raw_usage, "input_tokens_details", None)
    output_details = _field(raw_usage, "output_tokens_details", None)
    cached_tokens = _token_count(_field(input_details, "cached_tokens", 0))
    cache_write_tokens = _token_count(
        _field(input_details, "cache_write_tokens", 0)
    )
    estimated = input_tokens is None or output_tokens is None
    if estimated:
        from context.token_budget import estimate_tokens
        if input_tokens is None:
            input_tokens = sum(
                estimate_tokens(str(item.get("content") or item.get("output") or ""))
                for item in input_items
            )
        if output_tokens is None:
            output_tokens = estimate_tokens(output_text)
    return TokenUsage(
        input_tokens=_token_count(input_tokens),
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=_token_count(output_tokens),
        reasoning_tokens=_token_count(
            _field(output_details, "reasoning_tokens", 0)
        ),
        estimated=estimated,
    )


def _parse_stream_fallback(
    *,
    output_items: dict[int, Any],
    argument_deltas: dict[str, str],
    full_text: str,
    full_reasoning: str,
    input_items: list[dict[str, Any]],
) -> LLMResponse:
    items = [output_items[index] for index in sorted(output_items)]
    call = next(
        (item for item in items if _field(item, "type", "") == "function_call"),
        None,
    )
    if call is not None:
        arguments = _field(call, "arguments", "")
        item_id = _string_value(_field(call, "id", ""))
        call_id = _string_value(_field(call, "call_id", ""))
        arguments = arguments or argument_deltas.get(item_id, "")
        arguments = arguments or argument_deltas.get(call_id, "") or "{}"
        call = {
            "type": "function_call",
            "name": _field(call, "name", ""),
            "arguments": arguments,
        }
        action = _function_call_action(call, full_reasoning or full_text)
    elif full_text:
        action = Action(ActionType.FINISH, full_reasoning, message=full_text)
    else:
        action = Action(
            ActionType.GIVE_UP,
            full_reasoning,
            message="Responses stream ended without a completed response",
        )

    return LLMResponse(
        action=action,
        raw_content=full_text,
        usage=_response_usage(None, input_items, full_text),
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
