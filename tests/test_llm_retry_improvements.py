from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.core import Agent, AgentConfig
from agent.task import Action, ActionType
from llm.base import LLMMessage, MockBackend
from llm.errors import LLMCallbackError, LLMErrorKind, classify_llm_error
from tools.base import NoopTool, ToolRegistry


class HTTPError(Exception):
    def __init__(self, status_code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def make_agent(backend, **config):
    registry = ToolRegistry().register(NoopTool("shell"))
    return Agent(backend, registry, AgentConfig(**config))


def finish_backend():
    return MockBackend([Action(ActionType.FINISH, "done", message="ok")])


def test_classifier_retries_only_known_transient_errors():
    assert classify_llm_error(ConnectionError("network down")).retryable
    assert classify_llm_error(HTTPError(503, "unavailable")).retryable
    assert classify_llm_error(HTTPError(429, "limited")).kind == LLMErrorKind.RATE_LIMIT
    assert not classify_llm_error(RuntimeError("something went wrong")).retryable
    assert not classify_llm_error(HTTPError(422, "invalid input")).retryable


def test_retry_after_is_honored_but_capped():
    backend = finish_backend()
    original = backend.complete
    attempts = 0

    def complete(messages, tools):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(429, "rate limited", {"Retry-After": "12"})
        return original(messages, tools)

    backend.complete = complete
    agent = make_agent(
        backend,
        llm_max_retries=2,
        llm_retry_delay=1,
        llm_retry_max_delay=5,
    )
    with patch("time.sleep") as sleep:
        agent._call_with_retry([LLMMessage("user", "go")], [])

    sleep.assert_called_once_with(5)


def test_jitter_is_added_within_max_delay():
    backend = finish_backend()
    original = backend.complete
    attempts = 0

    def complete(messages, tools):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("network timeout")
        return original(messages, tools)

    backend.complete = complete
    agent = make_agent(
        backend,
        llm_max_retries=2,
        llm_retry_delay=2,
        llm_retry_max_delay=10,
        llm_retry_jitter=0.5,
    )
    with patch("random.uniform", return_value=0.75), patch("time.sleep") as sleep:
        agent._call_with_retry([LLMMessage("user", "go")], [])

    sleep.assert_called_once_with(2.75)


def test_unknown_exception_is_not_retried():
    backend = finish_backend()
    attempts = 0

    def complete(messages, tools):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("unknown provider failure")

    backend.complete = complete
    agent = make_agent(backend, llm_max_retries=3, llm_retry_delay=0)

    with pytest.raises(RuntimeError):
        agent._call_with_retry([LLMMessage("user", "go")], [])
    assert attempts == 1


def test_callback_exception_is_not_retried():
    backend = finish_backend()
    attempts = 0

    def stream(messages, tools, on_text=None, on_thought=None):
        nonlocal attempts
        attempts += 1
        on_text("prefix")
        raise AssertionError("unreachable")

    def callback(_chunk):
        raise ValueError("renderer failed")

    backend.stream = stream
    agent = make_agent(
        backend,
        stream=True,
        stream_callback=callback,
        llm_max_retries=3,
        llm_retry_delay=0,
    )

    with pytest.raises(LLMCallbackError, match="renderer failed"):
        agent._call_with_retry([LLMMessage("user", "go")], [])
    assert attempts == 1


def test_stream_failure_after_visible_chunk_is_not_retried():
    backend = finish_backend()
    attempts = 0
    chunks = []

    def stream(messages, tools, on_text=None, on_thought=None):
        nonlocal attempts
        attempts += 1
        on_text("prefix")
        raise ConnectionError("stream interrupted")

    backend.stream = stream
    agent = make_agent(
        backend,
        stream=True,
        stream_callback=chunks.append,
        llm_max_retries=3,
        llm_retry_delay=0,
    )

    with pytest.raises(ConnectionError):
        agent._call_with_retry([LLMMessage("user", "go")], [])
    assert attempts == 1
    assert chunks == ["prefix"]


def test_stream_failure_before_first_chunk_can_retry():
    backend = finish_backend()
    original = backend.complete
    attempts = 0

    def stream(messages, tools, on_text=None, on_thought=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("connect failed")
        on_text("ok")
        return original(messages, tools)

    backend.stream = stream
    agent = make_agent(
        backend,
        stream=True,
        llm_max_retries=2,
        llm_retry_delay=0,
    )

    result = agent._call_with_retry([LLMMessage("user", "go")], [])
    assert result.action.action_type == ActionType.FINISH
    assert attempts == 2