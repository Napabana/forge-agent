"""Cross-backend usage aggregation tests. No real API calls are made."""

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog, summarize_run
from agent.task import Action, ActionType, Task, ToolCall
from entry.chat import _format_usage
from llm.base import LLMResponse, MockBackend
from llm.usage import SessionUsage, TokenUsage
from tools.base import NoopTool, ToolRegistry


def test_usage_total_does_not_double_count_reasoning() -> None:
    usage = TokenUsage(
        input_tokens=100,
        cached_tokens=20,
        cache_write_tokens=10,
        output_tokens=50,
        reasoning_tokens=15,
    )

    assert usage.total_tokens == 150


def test_llm_response_keeps_legacy_constructor_compatible() -> None:
    response = LLMResponse(
        action=Action(ActionType.FINISH, "done", message="done"),
        raw_content="done",
        input_tokens=10,
        output_tokens=5,
    )

    assert response.total_tokens == 15


def test_session_usage_records_calls_and_estimates() -> None:
    usage = SessionUsage()
    usage.record(TokenUsage(input_tokens=10, output_tokens=5))
    usage.record(TokenUsage(input_tokens=20, output_tokens=7, estimated=True))

    assert usage.llm_calls == 2
    assert usage.estimated_calls == 1
    assert usage.total_tokens == 42


def test_chat_usage_format_shows_experiment_breakdown() -> None:
    usage = SessionUsage(
        llm_calls=2,
        input_tokens=100,
        cached_tokens=20,
        cache_write_tokens=10,
        output_tokens=50,
        reasoning_tokens=15,
    )

    text = _format_usage(usage)

    assert "150 tokens" in text
    assert "2 calls" in text
    assert "input 100" in text
    assert "cached 20" in text
    assert "cache-write 10" in text
    assert "output 50" in text
    assert "reasoning 15" in text
    assert "cache-hit 20.0%" in text


def test_agent_and_event_log_keep_per_call_usage(tmp_path) -> None:
    backend = MockBackend(
        [
            Action(ActionType.TOOL_CALL, "inspect", ToolCall("noop", {})),
            Action(ActionType.FINISH, "done", message="done"),
        ],
        input_tokens=100,
        cached_tokens=20,
        cache_write_tokens=10,
        output_tokens=50,
        reasoning_tokens=15,
    )
    registry = ToolRegistry().register(NoopTool("noop"))
    agent = Agent(backend, registry, AgentConfig(max_steps=2))
    task = Task(description="inspect", repo_path=str(tmp_path), max_steps=2)

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = agent.run(task, log)
        summary = summarize_run(log)

    assert result.usage.llm_calls == 2
    assert result.usage.input_tokens == 200
    assert result.usage.cached_tokens == 40
    assert result.usage.cache_write_tokens == 20
    assert result.usage.output_tokens == 100
    assert result.usage.reasoning_tokens == 30
    assert result.total_tokens == 300
    assert summary["usage"] == result.usage.to_dict()
