"""Model-visible execution workspace regressions for local and sandbox runs."""

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, RunStatus, Task
from llm.base import MockBackend
from tools.base import ToolRegistry
from tools.runtime import CONTAINER_WORKDIR


def _visible_prompt(tmp_path, execution_workspace=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    backend = MockBackend([Action(ActionType.FINISH, "done", message="done")])
    task = Task(description="inspect and finish", repo_path=str(repo), max_steps=1)
    config = AgentConfig(repo_map_mode="none", execution_workspace=execution_workspace)
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = Agent(backend, ToolRegistry(), config).run(task, log)
    visible = "\n".join(message.content for message in backend.received_messages[0])
    return result, visible, str(repo)


def test_sandbox_model_context_uses_container_workspace_not_host_path(tmp_path):
    result, visible, host_repo = _visible_prompt(tmp_path, CONTAINER_WORKDIR)
    assert result.status is RunStatus.SUCCESS
    assert CONTAINER_WORKDIR in visible
    assert host_repo not in visible
    assert "Shell commands already start" in visible
    assert "do not `cd` to host filesystem paths" in visible


def test_local_model_context_keeps_host_repo_path_and_no_sandbox_hint(tmp_path):
    result, visible, host_repo = _visible_prompt(tmp_path)
    assert result.status is RunStatus.SUCCESS
    assert host_repo in visible
    assert "Shell commands already start" not in visible
