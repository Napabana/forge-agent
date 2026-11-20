import subprocess
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from agent.task import Action, ActionType, Event, EventType as E
from config.schema import AppConfig
from entry.cli import cli
from entry.ui.terminal import TerminalUI
from llm.base import MockBackend


def test_chat_commands_follow_the_turn_and_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    # Git for Windows may see an ownerless mounted drive. Trust only this fixture.
    git_config = tmp_path / "gitconfig"
    subprocess.run(["git", "config", "--file", str(git_config), "safe.directory", repo.as_posix()], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    path = repo / "file"
    path.write_text("old\n")
    subprocess.run(["git", "add", "file"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], cwd=repo, check=True)
    path.write_text("new\n")
    config = AppConfig()
    config.agent.log_dir = str(tmp_path / "logs")
    backend = MockBackend([Action(ActionType.FINISH, "", message=reply) for reply in ("first reply", "next reply")])
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: config)
    monkeypatch.setattr("entry.cli._chat_backend", lambda cfg: backend)
    result = CliRunner().invoke(cli, ["chat", "--repo", str(repo), "--no-session"],
                                input="read-only first\n/diff\n/tokens\n/status\n/compact\n/plan\n/skills\n/mcp\n"
                                      "/new\n/diff\nread-only second\n/exit\n", catch_exceptions=False)
    assert result.exit_code == 0 and "\x1b" not in result.output
    for expected in ("+new", "Latest model input: 100", "150 tokens", "CHAT STATUS", "SKILLS", "MCP",
                     "No compact event", "No plan event", "No patch recorded"):
        assert expected in result.output
    assert result.output.count("first reply") == result.output.count("next reply") == 1
    assert result.output.index("first reply") < result.output.index("next reply")


def test_permission_boundaries_and_interleaved_stream(capsys):
    ui = TerminalUI(chat=True)
    ui.on_reasoning_text("reason")
    ui.on_stream_text("first")
    ui(Event(E.SKILL_LOADED, "test", {"skill": "inspect"}))
    def prompt(text):
        assert ui.live is None
        assert text == "original command"
        return False
    assert ui.confirm_with(prompt)("original command") is False
    ui.on_stream_text(" second")
    ui(Event(E.TASK_COMPLETE, "test", {"summary": "first second"}))
    out = capsys.readouterr().out
    assert out.count("first") == out.count("second") == 1
    assert out.index("reason\n") < out.index("first\n") < out.index("Skill loaded")


def test_prompt_multiline_history_and_editor_binding(tmp_path, monkeypatch):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from entry import chat_input

    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(chat_input, "sys", SimpleNamespace(stdin=tty, stdout=tty, stderr=tty))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    with create_pipe_input() as input_pipe:
        monkeypatch.setattr(chat_input, "PromptSession", lambda **kw: PromptSession(
            input=input_pipe, output=DummyOutput(), **kw))
        session = chat_input.chat_prompt(tmp_path)
        # Replace launching a desktop editor with a deterministic edit of the same buffer.
        monkeypatch.setattr(session.default_buffer, "open_in_editor",
                            lambda: session.default_buffer.insert_text("edited"))
        input_pipe.send_text("one\x1b\rtwo\x07\r")
        assert session.prompt("> ") == "one\ntwoedited"
        def recall_after_history_load():
            buffer = session.default_buffer
            buffer.load_history_if_not_yet_loaded()
            buffer._load_history_task.add_done_callback(lambda _: input_pipe.send_text("\x1b[A\r"))
        assert session.prompt("> ", pre_run=recall_after_history_load) == "one\ntwoedited"
