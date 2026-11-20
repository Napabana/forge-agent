from copy import deepcopy

from agent.task import Event, EventType as E
from config.schema import UIConfig
from entry.ui.presenter import present
from entry.ui.terminal import TerminalUI


def test_all_known_events_have_a_verbose_projection_or_are_aggregated():
    for kind in E:
        event = Event(kind, "test", {})
        projection = present(event, UIConfig(), verbose=True)
        assert projection is not None or kind == E.MCP_TOOL_DISCOVERED
    assert present(Event("future-event", "test", {}), UIConfig()) is None


def test_payload_and_trace_secrets_never_become_display_state(capsys):
    event = Event(E.ACTION, "test", {"action": {"thought": "private reasoning", "action_type": "tool_call",
                  "tool_call": {"name": "shell", "params": {"cmd": "export API_KEY=privatevalue",
                                                           "token": "hidden"}}}})
    original = deepcopy(event.payload)
    ui = TerminalUI()
    ui(event)
    ui(event)
    out = capsys.readouterr().out
    assert out.count("tool=shell") == 1
    assert all(value not in out for value in ("private reasoning", "privatevalue", "hidden"))
    assert event.payload == original


def test_tool_output_preview_duration_and_verbose(capsys):
    ui = TerminalUI(UIConfig(tool_output_preview_lines=2))
    for status in ("success", "error", "timeout"):
        ui(Event(E.TOOL_EXECUTION_FINISHED, "task", {"step": 5, "tool_name": "test", "duration_ms": 1250}))
        event = Event(E.OBSERVATION, "task", {"step": 5, "observation": {"tool_name": "test", "status": status,
                      "output": "first\nsecond\nthird", "error": None if status == "success" else "failure"}})
        ui(event)
        text = capsys.readouterr().out
        assert "1.25s" in text and "first" in text and "third" not in text
        assert ("second" in text) == (status != "success")
        assert ("Error: failure" in text) == (status != "success")
        assert "more lines" in text
        assert "third" in present(event, UIConfig(), verbose=True).detail
