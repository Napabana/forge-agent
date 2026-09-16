from __future__ import annotations

import os

from agent.task import Action, ActionType
from config.schema import AppConfig
from entry.chat import ChatSession
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


def _config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.agent.max_steps = 4
    cfg.agent.budget_tokens = 40_000
    cfg.agent.log_dir = str(tmp_path / "logs")
    cfg.context.history_window = 20
    os.makedirs(cfg.agent.log_dir, exist_ok=True)
    return cfg


def test_chat_query_change_reranks_same_persistent_index_without_reparse(tmp_path):
    (tmp_path / "alpha.py").write_text("def alpha_handler():\n    return 'alpha'\n")
    (tmp_path / "beta.py").write_text("def beta_handler():\n    return 'beta'\n")
    received_system: list[str] = []

    class RecordingBackend(MockBackend):
        def complete(self, messages, tools):
            received_system.append(messages[0].content)
            return super().complete(messages, tools)

    backend = RecordingBackend([
        Action(ActionType.FINISH, "round one", message="done one"),
        Action(ActionType.FINISH, "round two", message="done two"),
    ])
    session = ChatSession(
        backend=backend,
        registry=ToolRegistry().register(NoopTool("shell")),
        config=_config(tmp_path),
        repo_path=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
        stream=False,
        prepare_next_turn=lambda _context: None,
    )

    original_new_repo_map = session.agent._new_repo_map
    constructor_calls = 0

    def counting_new_repo_map(repo_path):
        nonlocal constructor_calls
        constructor_calls += 1
        return original_new_repo_map(repo_path)

    session.agent._new_repo_map = counting_new_repo_map

    assert session.run_round("change the alpha handler")
    repo_map_instance = session.agent._repo_map_instance
    assert session.run_round("now inspect the beta handler")

    assert len(received_system) == 2
    assert received_system[0].index("alpha.py") < received_system[0].index("beta.py")
    assert received_system[1].index("beta.py") < received_system[1].index("alpha.py")
    assert session.agent._repo_map_instance is repo_map_instance
    assert constructor_calls == 1

    metrics = repo_map_instance.metrics
    assert len(metrics.full_rebuild_seconds) == 1
    assert metrics.incremental_update_seconds == []
    assert metrics.warm_load_seconds == []
