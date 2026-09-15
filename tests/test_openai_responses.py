"""OpenAI Responses backend tests. No real API calls are made."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.task import ActionType
from llm.base import LLMMessage, LLMToolSchema


def _backend(model="gpt-5.4"):
    with patch("openai.OpenAI"):
        from llm.openai_responses import OpenAIResponsesBackend
        return OpenAIResponsesBackend(model=model, api_key="sk-test")


def _usage(input_tokens=80, output_tokens=40):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _response(*, text="", output=None, usage=None, status="completed", error=None):
    return SimpleNamespace(
        output_text=text,
        output=output or [],
        usage=usage,
        status=status,
        error=error,
    )


def _tool(name="shell"):
    return LLMToolSchema(
        name=name,
        description="run a command",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    )


class TestResponsesComplete:
    def test_text_response_is_finish(self):
        backend = _backend()
        backend._client.responses.create.return_value = _response(
            text="Task complete",
            usage=_usage(),
        )

        result = backend.complete([LLMMessage("user", "go")], [])

        assert result.action.action_type == ActionType.FINISH
        assert result.action.message == "Task complete"
        assert result.input_tokens == 80
        assert result.output_tokens == 40

        kwargs = backend._client.responses.create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.4"
        assert kwargs["input"] == [{"role": "user", "content": "go"}]
        assert kwargs["max_output_tokens"] == 4096
        assert "stream" not in kwargs

    def test_function_call_is_tool_action(self):
        backend = _backend()
        call = SimpleNamespace(
            type="function_call",
            name="shell",
            arguments='{"cmd": "pytest"}',
        )
        backend._client.responses.create.return_value = _response(
            output=[call],
            usage=_usage(),
        )

        result = backend.complete([LLMMessage("user", "test")], [_tool()])

        assert result.action.action_type == ActionType.TOOL_CALL
        assert result.action.tool_call.name == "shell"
        assert result.action.tool_call.params == {"cmd": "pytest"}

        tool = backend._client.responses.create.call_args.kwargs["tools"][0]
        assert tool["type"] == "function"
        assert tool["name"] == "shell"
        assert "function" not in tool
        assert tool["parameters"]["type"] == "object"

    def test_usage_details_are_disjoint_and_not_double_counted(self):
        backend = _backend()
        usage = SimpleNamespace(
            input_tokens=100,
            input_tokens_details=SimpleNamespace(
                cached_tokens=20,
                cache_write_tokens=10,
            ),
            output_tokens=50,
            output_tokens_details=SimpleNamespace(reasoning_tokens=15),
        )
        backend._client.responses.create.return_value = _response(
            text="done",
            usage=usage,
        )

        result = backend.complete([LLMMessage("user", "go")], [])

        assert result.input_tokens == 100
        assert result.cached_tokens == 20
        assert result.cache_write_tokens == 10
        assert result.output_tokens == 50
        assert result.reasoning_tokens == 15
        assert result.total_tokens == 150

    def test_output_text_falls_back_to_message_parts(self):
        backend = _backend()
        message = {
            "type": "message",
            "content": [
                {"type": "output_text", "text": "from content part"},
            ],
        }
        backend._client.responses.create.return_value = {
            "output_text": "",
            "output": [message],
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "status": "completed",
        }

        result = backend.complete([LLMMessage("user", "go")], [])

        assert result.action.action_type == ActionType.FINISH
        assert result.action.message == "from content part"

    def test_missing_output_returns_give_up(self):
        backend = _backend()
        backend._client.responses.create.return_value = _response(
            status="failed",
            error=SimpleNamespace(message="upstream failed"),
        )

        result = backend.complete([LLMMessage("user", "go")], [])

        assert result.action.action_type == ActionType.GIVE_UP
        assert result.action.message == "upstream failed"

    def test_missing_usage_is_estimated(self):
        backend = _backend()
        backend._client.responses.create.return_value = _response(text="done")

        result = backend.complete([LLMMessage("user", "hello world")], [])

        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.usage.estimated is True

    def test_tool_result_input_uses_function_call_output(self):
        backend = _backend()
        backend._client.responses.create.return_value = _response(
            text="done",
            usage=_usage(),
        )
        messages = [
            LLMMessage(
                role="tool",
                content="tests passed",
                tool_call_id="call_123",
            )
        ]

        backend.complete(messages, [])

        assert backend._client.responses.create.call_args.kwargs["input"] == [{
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "tests passed",
        }]


class TestResponsesStream:
    def test_text_and_reasoning_events_are_streamed(self):
        backend = _backend()
        final = _response(text="hello", usage=_usage())
        events = [
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="think ",
            ),
            SimpleNamespace(type="response.output_text.delta", delta="hel"),
            SimpleNamespace(type="response.output_text.delta", delta="lo"),
            SimpleNamespace(type="response.completed", response=final),
        ]
        backend._client.responses.create.return_value = iter(events)
        text = []
        thought = []

        result = backend.stream(
            [LLMMessage("user", "go")],
            [],
            on_text=text.append,
            on_thought=thought.append,
        )

        assert result.action.action_type == ActionType.FINISH
        assert result.action.message == "hello"
        assert result.action.thought == "think "
        assert text == ["hel", "lo"]
        assert thought == ["think "]
        assert backend._client.responses.create.call_args.kwargs["stream"] is True

    def test_stream_tool_call_fallback_without_completed_event(self):
        backend = _backend()
        call = SimpleNamespace(
            type="function_call",
            id="item_1",
            call_id="call_1",
            name="shell",
            arguments="",
        )
        events = [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=call,
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                item_id="item_1",
                delta='{"cmd":',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                item_id="item_1",
                delta='"pytest"}',
            ),
        ]
        backend._client.responses.create.return_value = iter(events)

        result = backend.stream([LLMMessage("user", "test")], [_tool()])

        assert result.action.action_type == ActionType.TOOL_CALL
        assert result.action.tool_call.name == "shell"
        assert result.action.tool_call.params == {"cmd": "pytest"}


class TestResponsesRouting:
    def test_responses_protocol_selects_responses_backend(self):
        from llm.router import create_backend
        with patch("llm.openai_responses.OpenAIResponsesBackend.__init__", return_value=None):
            backend = create_backend(
                "openai",
                "gpt-5.4",
                api_key="sk-test",
                protocol="responses",
            )
        from llm.openai_responses import OpenAIResponsesBackend
        assert isinstance(backend, OpenAIResponsesBackend)

    def test_default_protocol_keeps_chat_completions_backend(self):
        from llm.router import create_backend
        with patch("llm.openai_compat.OpenAICompatBackend.__init__", return_value=None):
            backend = create_backend("openai", "gpt-4o", api_key="sk-test")
        from llm.openai_compat import OpenAICompatBackend
        assert isinstance(backend, OpenAICompatBackend)

    def test_unknown_protocol_is_rejected(self):
        from llm.router import create_backend
        with pytest.raises(ValueError, match="Unsupported LLM protocol"):
            create_backend(
                "openai",
                "gpt-5.4",
                api_key="sk-test",
                protocol="unknown",
            )
