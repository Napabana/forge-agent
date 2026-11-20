from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from config.schema import _parse, load_config, save_selected_model, select_model
from context.compaction import TraceableCompaction
from entry.chat_input import ChatCompleter
from llm.base import MockBackend


def _choices(completer, text):
    return [item.text for item in completer.get_completions(Document(text), CompleteEvent())]


def test_repository_path_completion_leaves_slash_commands_to_the_inline_list(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    completer = ChatCompleter(tmp_path)

    assert _choices(completer, "/") == []
    assert _choices(completer, "/mo") == []
    assert _choices(completer, "/model ") == []
    assert "src/" in _choices(completer, "open @")
    assert _choices(completer, "open @src/m") == ["main.py"]
    assert _choices(completer, "open @../") == []


def test_model_profile_changes_connection_and_is_saved_without_editing_yaml(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".config" / "forge-agent"
    config_dir.mkdir(parents=True)
    yaml_path = config_dir / "config.yaml"
    original = """llm:
  provider: ollama
  model: first
models:
  second:
    provider: openai
    model: second-model
    base_url: https://example.invalid/v1
    max_tokens: 1024
"""
    yaml_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    config = load_config()
    assert config.llm.model == "first"
    select_model(config, "second")
    assert (config.llm.provider, config.llm.model, config.llm.base_url) == (
        "openai", "second-model", "https://example.invalid/v1"
    )
    assert config.llm.api_key == ""
    assert config.llm.max_output_tokens == 1024
    save_selected_model("second", profile=True)
    assert load_config().llm.model == "second-model"
    assert load_config(yaml_path).llm.model == "first"
    save_selected_model("another-id")
    assert load_config().llm.model == "another-id"
    assert yaml_path.read_text(encoding="utf-8") == original


def test_model_profile_rejects_unknown_name_without_changing_config():
    config = _parse({"llm": {"provider": "ollama", "model": "first"}})
    before = replace(config.llm)
    try:
        select_model(config, "missing")
    except ValueError:
        pass
    else:
        assert False, "missing profile should fail"
    assert config.llm == before


def test_compaction_switches_semantic_backend():
    policy = TraceableCompaction()
    first = object()
    second = object()
    policy.bind_backend(first)
    policy.bind_backend(second)
    assert policy.semantic_summarizer.backend is second
    policy.bind_backend(MockBackend([]))
    assert policy.semantic_summarizer.backend is second


@pytest.mark.parametrize(("picker", "save"), [(False, True), (True, False), (True, True)])
def test_chat_model_command_switches_backend_and_saves_only_when_requested(tmp_path, monkeypatch, picker, save):
    from click.testing import CliRunner
    from entry.cli import cli

    config = _parse({"llm": {"provider": "ollama", "model": "first"},
                     "models": {"second": {"model": "second-model"}}})
    config.agent.log_dir = str(tmp_path / "logs")
    seen = {}

    class FakeSession:
        approval_mode = "ask"

        def __init__(self, **kwargs):
            seen["session"] = self
            seen["initial"] = kwargs["backend"]

        def switch_backend(self, backend):
            seen["switched"] = backend

        def close(self):
            pass

        def print_stats(self):
            pass

    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: config)
    monkeypatch.setattr("entry.cli._chat_backend", lambda cfg: cfg.llm.model)
    if picker:
        from entry.ui.terminal import make_console

        def tty_console(config):
            console, _ = make_console(config)
            return console, True

        commands = iter(("/model", "/exit"))
        monkeypatch.setattr("entry.ui.terminal.make_console", tty_console)
        monkeypatch.setattr("entry.chat_input.chat_prompt", lambda *_: SimpleNamespace(prompt=lambda _: next(commands)))
        monkeypatch.setattr("entry.chat_input.choose_model", lambda *_, **kwargs: ("second", save))
    monkeypatch.setattr("entry.chat.ChatSession", FakeSession)
    monkeypatch.setattr("config.schema.save_selected_model", lambda name, **kwargs: seen.setdefault("saved", name))
    result = CliRunner().invoke(cli, ["chat", "--repo", str(tmp_path), "--no-session"],
                                input="" if picker else "/model second\n/exit\n", catch_exceptions=False)

    assert result.exit_code == 0
    assert seen["initial"] == "first"
    assert seen["switched"] == "second-model"
    assert seen.get("saved") == ("second" if save else None)
    assert config.llm.model == "second-model"


def test_chat_rewind_command_switches_to_a_saved_branch(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from agent.session_store import JsonChatSessionStore
    from agent.task import Action, ActionType
    from entry.cli import cli

    config = _parse({"llm": {"provider": "ollama", "model": "fixture"}, "context": {"compaction_mode": "off"}})
    config.agent.log_dir = str(tmp_path / "logs")
    backend = MockBackend([Action(ActionType.FINISH, "", message="fixture reply")])
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: config)
    monkeypatch.setattr("entry.cli._chat_backend", lambda cfg: backend)
    monkeypatch.setattr("entry.chat_input.chat_prompt", lambda *args: None)
    result = CliRunner().invoke(cli, ["chat", "--repo", str(tmp_path)], input="first question\n/rewind 1\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Rewound to round 1 in session " in result.output
    state = JsonChatSessionStore(tmp_path / "logs" / "chat").latest_for_repo(tmp_path)
    assert state.forked_from_session_id and state.forked_from_round == 1


def test_config_show_redacts_profile_keys(tmp_path):
    from click.testing import CliRunner
    from entry.cli import cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text("""llm:
  provider: openai
  model: first
  api_key: private-first
models:
  second:
    model: second-model
    api_key: private-second
""", encoding="utf-8")
    result = CliRunner().invoke(cli, ["--config", str(config_path), "config", "show"])
    assert result.exit_code == 0
    assert "private-first" not in result.output
    assert "private-second" not in result.output
