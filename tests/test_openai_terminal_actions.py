from __future__ import annotations

from types import SimpleNamespace

from agent.prompt import build_system_prompt, build_task_prompt
from agent.task import ActionType
from llm.openai_compat import _parse_openai_response


def _tool_choice(name: str, arguments: str = "{}"):
    function = SimpleNamespace(name=name, arguments=arguments)
    tool_call = SimpleNamespace(function=function)
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(finish_reason="tool_calls", message=message)


def _stop_choice(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(finish_reason="stop", message=message)


def test_finish_function_call_is_normalized_to_terminal_action():
    action = _parse_openai_response(
        _tool_choice("finish", '{"summary":"Implemented and verified the fix."}'),
        "(no thought)",
    )

    assert action.action_type == ActionType.FINISH
    assert action.tool_call is None
    assert action.message == "Implemented and verified the fix."


def test_give_up_function_call_is_normalized_to_terminal_action():
    action = _parse_openai_response(
        _tool_choice("give_up", '{"reason":"Missing required repository data."}'),
        "(no thought)",
    )

    assert action.action_type == ActionType.GIVE_UP
    assert action.tool_call is None
    assert action.message == "Missing required repository data."


def test_regular_function_call_remains_a_tool_call():
    action = _parse_openai_response(
        _tool_choice("file_read", '{"path":"foo.py"}'),
        "inspect the target file",
    )

    assert action.action_type == ActionType.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.name == "file_read"
    assert action.tool_call.params == {"path": "foo.py"}


def test_explicit_give_up_stop_response_is_not_misclassified_as_finish():
    action = _parse_openai_response(
        _stop_choice("GIVE_UP: required context is unavailable"),
        "GIVE_UP: required context is unavailable",
    )

    assert action.action_type == ActionType.GIVE_UP
    assert action.message == "required context is unavailable"


def test_prompts_do_not_describe_finish_or_give_up_as_tools():
    system_prompt = build_system_prompt("/tmp/repo", [])
    task_prompt = build_task_prompt("Fix it", "/tmp/repo")
    combined = f"{system_prompt}\n{task_prompt}".lower()

    assert "call finish" not in combined
    assert "call give_up" not in combined
    assert "do not call a `finish` or `give_up` tool" in system_prompt.lower()
