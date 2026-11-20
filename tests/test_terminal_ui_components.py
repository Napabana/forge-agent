import io
from types import SimpleNamespace

import pytest

from config.schema import UIConfig, _parse
from entry.ui.terminal import TerminalUI


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    stream = Tty()
    from rich.console import Console
    monkeypatch.setattr("entry.ui.terminal.sys", SimpleNamespace(stdout=stream, stderr=Tty()))
    monkeypatch.setattr("entry.ui.terminal.Console",
                        lambda **kwargs: Console(file=stream, legacy_windows=False, **kwargs))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    return stream


@pytest.mark.parametrize("width", [40, 60, 80, 120])
def test_header_markdown_diff_and_resize(terminal, width):
    ui = TerminalUI(UIConfig(theme="light"), chat=True)
    assert ui.rich and ui.progress
    ui.console.width = width
    ui.header("FORGE AGENT", {"Task": "读取测试文件 " * 50, "Model": "fixture"})
    ui.status("Waiting for model")
    ui.console.width = 37
    ui.console.height = 5
    ui.on_stream_text("**hello**\n\n```python\nprint(42)\n```")
    ui.close()
    ui.diff("--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new")
    output = terminal.getvalue()
    assert all(word in output for word in ("FORGE AGENT", "fixture", "hello", "42", "+new"))
    assert ui.live is None and "\x1b[?25h" in output
    assert "\x1b[?1049h" not in output


@pytest.mark.parametrize("env,value,rich", [("TERM", "dumb", False), ("CI", "true", False),
                                            ("NO_COLOR", "", True)])
def test_capability_downgrade(terminal, monkeypatch, env, value, rich):
    monkeypatch.setenv(env, value)
    ui = TerminalUI()
    assert ui.rich is rich
    ui.panel("Example", {"Model": "fixture"})
    assert "\x1b[" not in terminal.getvalue()


@pytest.mark.parametrize("values", [{"mode": "screen"}, {"theme": "yellow"}, {"animations": "false"},
                                    {"live_refresh_per_second": 11}, {"tool_output_preview_lines": True},
                                    {"tool_output_mode": "raw"}, {"unsupported": 123}])
def test_invalid_ui_config(values):
    with pytest.raises(ValueError, match="ui"):
        _parse({"ui": values})


def test_live_pauses_for_permission_and_restores_even_on_interrupt(terminal):
    ui = TerminalUI(UIConfig(animations=True))
    ui.status("Executing tool")
    def interrupted(prompt):
        assert ui.live is None
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ui.confirm_with(interrupted)("command")
    assert ui.live is not None and ui.live.is_started
    ui.close()
    assert "Waiting for permission" in terminal.getvalue()
