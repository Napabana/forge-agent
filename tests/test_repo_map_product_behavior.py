from __future__ import annotations

import os
import subprocess

from agent.task import Action, ActionType, ToolCall
from config.schema import AppConfig
from entry.chat import ChatSession
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileViewTool, FileWriteTool


def _config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.agent.max_steps = 4
    cfg.agent.budget_tokens = 40_000
    cfg.agent.log_dir = str(tmp_path / "logs")
    cfg.context.history_window = 20
    os.makedirs(cfg.agent.log_dir, exist_ok=True)
    return cfg


def test_chat_query_change_reranks_same_persistent_index_without_reparse(tmp_path, monkeypatch):
    # 冷构建断言必须使用本次测试独有缓存，不能复用用户目录里的旧索引。
    monkeypatch.setenv("FORGE_AGENT_CACHE_DIR", str(tmp_path / "logs" / "index-cache"))
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


def test_chat_shares_warm_repo_snapshot_and_refreshes_after_edits(tmp_path, monkeypatch):
    import agent.core as core
    import context.incremental_repo_map as incremental

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("FORGE_AGENT_CACHE_DIR", str(tmp_path / "index"))
    target = repo / "handler.py"
    target.write_text("def original_handler():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "handler.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "fixture"], cwd=repo, check=True)
    finish = Action(ActionType.FINISH, "", message="done")
    backend = MockBackend([
        finish,
        Action(ActionType.TOOL_CALL, "read", ToolCall("file_view", {"path": "handler.py"})),
        finish,
        Action(ActionType.TOOL_CALL, "write", ToolCall("file_write", {
            "path": "new_handler.py", "content": "def new_handler():\n    return 3\n",
        })),
        finish,
    ])
    session = ChatSession(backend, ToolRegistry().register(FileViewTool(workspace=str(repo)))
                          .register(FileWriteTool(workspace=str(repo))),
                          _config(tmp_path), str(repo), str(tmp_path / "logs"), stream=False,
                          prepare_next_turn=lambda _context: None)
    original_read = core.capture_repository_state
    snapshots = []
    full_hashes = []
    original_hash = session.agent._get_repo_content_state

    def capture(path):
        snapshots.append(path)
        return original_read(path)

    def content_hash(path, **kwargs):
        full_hashes.append(path)
        return original_hash(path, **kwargs)

    monkeypatch.setattr(core, "capture_repository_state", capture)
    monkeypatch.setattr(incremental, "capture_repository_state", capture)
    monkeypatch.setattr(session.agent, "_get_repo_content_state", content_hash)
    assert session.run_round("inspect handler")
    assert not full_hashes
    snapshots.clear()
    target.write_text("def renamed_handler():\n    return 2\n", encoding="utf-8")

    assert session.run_round("read renamed_handler")
    assert snapshots == [str(repo)]
    assert not full_hashes
    assert "renamed_handler" in backend.received_messages[1][0].content
    assert "original_handler" not in backend.received_messages[1][0].content

    assert session.run_round("add new_handler")
    assert full_hashes
    assert "function new_handler" in backend.received_messages[-1][0].content
