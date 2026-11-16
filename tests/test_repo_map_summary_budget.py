from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, Task
from context.token_budget import TokenBudget
from llm.base import MockBackend
from tools.base import ToolRegistry


def test_product_initial_repo_map_uses_actual_summary_token_cap(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("\n".join(f"def function_{i}(): pass" for i in range(100)), encoding="utf-8")
    task = Task("Inspect module", str(repo), max_steps=1)
    agent = Agent(
        MockBackend([Action(ActionType.FINISH, "done", message="done")]),
        ToolRegistry(),
        AgentConfig(repo_map_mode="incremental", repo_map_summary_budget=120,
                    repo_map_cache_dir=str(tmp_path / "cache")),
    )
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        agent.run(task, log)
    assert TokenBudget(total=80_000).counter.count_text(agent._repo_map_cache) <= 120
    assert agent.repo_map_telemetry["build_calls"] >= 1
