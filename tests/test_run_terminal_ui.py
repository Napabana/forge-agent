import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent.task import Action, ActionType, RunResult, RunStatus
from config.schema import AppConfig
from entry.cli import cli
from entry.ui.terminal import TerminalUI
from llm.base import MockBackend


class StreamingBackend(MockBackend):
    def stream(self, messages, tools, on_text=None, on_thought=None):
        response = self.complete(messages, tools)
        if on_thought:
            on_thought("reasoning sample")
        if on_text:
            on_text("answer ")
            on_text("sample")
        return response


@pytest.mark.parametrize("flags,reasoning", [([], False), (["--no-stream"], False),
                                            (["--reasoning-stream", "--verbose"], True)])
def test_piped_run_and_trace(tmp_path, monkeypatch, flags, reasoning):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = AppConfig()
    config.agent.log_dir = str(tmp_path / "logs")
    backend = StreamingBackend([Action(ActionType.FINISH, "", message="answer sample")])
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: config)
    monkeypatch.setattr("entry.cli.create_backend_from_config", lambda _: backend)
    result = CliRunner().invoke(cli, ["run", "--task", "read-only inspection", "--repo", str(repo),
                                     "--ui", "plain", "--no-progress", *flags])
    assert result.exit_code == 0, result.output
    assert result.output.count("answer sample") == 1
    assert ("reasoning sample" in result.output) == reasoning
    assert "RUN SUMMARY" in result.output and "Status: SUCCESS" in result.output
    assert "Acceptance:" in result.output and "Delivery:" in result.output
    assert "\x1b" not in result.output
    trace = list((tmp_path / "logs").glob("*.jsonl"))
    events = [json.loads(line) for line in trace[0].read_text().splitlines()]
    assert any(e["event_type"] == "task_complete" for e in events)
    assert "RUN SUMMARY" not in str(events)


@pytest.mark.parametrize("status", list(RunStatus))
def test_final_summary_uses_independent_outcomes(status, capsys):
    result = RunResult("task", status, "explanation", 4, acceptance_status="failed",
                       delivery_status="not_requested", error="test failed")
    result.patch = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new"
    TerminalUI().finish(result, 1.25)
    out = capsys.readouterr().out
    assert f"Status: {status.value.upper()}" in out and "Steps: 4" in out
    assert "Acceptance: failed" in out and "Delivery: not_requested" in out
    assert "Tokens:" not in out and "file  +1 -1" in out and "explanation" in out


def test_broken_stream_presentation_preserves_real_result(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = AppConfig()
    config.agent.log_dir = str(tmp_path)
    backend = StreamingBackend([Action(ActionType.FINISH, "", message="answer sample")])
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: config)
    monkeypatch.setattr("entry.cli.create_backend_from_config", lambda _: backend)
    original = TerminalUI._print
    def broken_chunk(ui, item, **kwargs):
        if kwargs.get("end") == "":
            raise OSError("test write failed")
        return original(ui, item, **kwargs)
    monkeypatch.setattr(TerminalUI, "_print", broken_chunk)
    result = CliRunner().invoke(cli, ["run", "--repo", str(repo), "-t", "read only"])
    assert backend.call_count == 1 and result.exit_code == 0 and "Status: SUCCESS" in result.output
    assert "answer sample" in result.output


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("script"),
                    reason="requires a Linux pseudo-terminal")
def test_rich_run_in_real_pty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  provider: ollama\n  model: fixture\nagent:\n  log_dir: logs\n"
                      "memory:\n  enabled: false\n")
    child = (
        "from entry.cli import cli; from agent.task import Action, ActionType; from llm.base import MockBackend; "
        "cli.main(obj={'evaluation_backend_factory': lambda _: MockBackend(["
        "Action(ActionType.FINISH, '', message='Fixture answer')])})"
    )
    command = shlex.join([sys.executable, "-c", child, "--config", str(config), "run",
                          "--ui", "rich", "--repo", str(repo), "--task", "read-only inspection"])
    env = {**os.environ, "HOME": str(tmp_path), "TERM": "xterm-256color", "CI": "false",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "COLUMNS": "54", "LINES": "14"}
    result = subprocess.run(["script", "-qec", command, "/dev/null"], cwd=tmp_path, env=env, input=b"",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    assert result.returncode == 0, result.stdout.decode(errors="replace")
    assert all(marker in result.stdout for marker in (b"FORGE AGENT", b"RUN SUMMARY", b"SUCCESS", b"Fixture answer",
                                                      b"\x1b[?25l", b"\x1b[?25h"))
