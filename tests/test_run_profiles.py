import pytest
from click.testing import CliRunner

from agent.core import AgentConfig
from agent.runner import ExecutionRunner, RunRequest
from agent.task import Action, ActionType, Task, ToolCall
from entry.cli import cli
from llm.base import MockBackend
from tools.base import BaseTool, ToolRegistry, ToolResult


class WritingTool(BaseTool):
    def __init__(self, path):
        self.path = path

    @property
    def name(self):
        return "write_probe"

    @property
    def description(self):
        return "test write"

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, params):
        self.path.write_text("unsafe", encoding="utf-8")
        return ToolResult(True, "wrote")


def test_safe_profile_denies_mutating_tool(tmp_path):
    target = tmp_path / "written.txt"
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "write", tool_call=ToolCall("write_probe", {})),
        Action(ActionType.FINISH, "done", message="done"),
    ])
    runner = ExecutionRunner(
        backend=backend,
        registry=ToolRegistry().register(WritingTool(target)),
        config=AgentConfig(run_profile="safe", repo_map_mode="none"),
        log_dir=str(tmp_path / "logs"),
    )
    runner.run(RunRequest(Task("Inspect", str(tmp_path), max_steps=2)))
    assert not target.exists()


def test_sandboxed_profile_requires_docker(tmp_path):
    runner = ExecutionRunner(
        backend=MockBackend([]), registry=ToolRegistry(),
        config=AgentConfig(run_profile="sandboxed"), log_dir=str(tmp_path / "logs"),
    )
    with pytest.raises(ValueError, match="requires Docker"):
        runner.run(RunRequest(Task("Inspect", str(tmp_path))))


def test_config_show_redacts_key_and_applies_profile(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  api_key: secret-value\nagent:\n  finish_policy: advisory\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["--config", str(config), "--profile", "safe", "config", "show"])
    assert result.exit_code == 0, result.output
    assert '"run_profile": "safe"' in result.output
    assert '"finish_policy": "advisory"' in result.output
    assert "secret-value" not in result.output
