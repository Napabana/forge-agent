from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from agent.core import Agent
from context import repo_map
from context.history import ConversationHistory
from context.repo_map import FileInfo, RepoMap, Symbol
from context.token_budget import TokenBudget
from llm.base import LLMMessage, MockBackend
from tools.base import ToolRegistry


def test_language_load_failure_is_not_negative_cached(monkeypatch):
    repo_map._lang_cache.pop(".py", None)
    attempts = 0

    def import_module(_name):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModuleNotFoundError("not installed yet")

        class Module:
            @staticmethod
            def language():
                return object()

        return Module()

    class Language:
        def __init__(self, capsule):
            self.capsule = capsule

    monkeypatch.setattr(repo_map.importlib, "import_module", import_module)
    fake_tree_sitter = type("TreeSitter", (), {"Language": Language})
    with patch.dict(sys.modules, {"tree_sitter": fake_tree_sitter}):
        first, first_report = repo_map._load_language(".py")
        second, second_report = repo_map._load_language(".py")

    assert first is None
    assert not first_report.available
    assert second is not None
    assert second_report.available
    assert attempts == 2
    repo_map._lang_cache.pop(".py", None)


def test_scan_uses_one_language_snapshot_per_extension(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.py").write_text("def b(): pass\n")
    language = object()
    loads = []
    parser_languages = []

    def load(ext, *, force_refresh=False):
        loads.append((ext, force_refresh))
        return language, repo_map.LanguageLoadReport(ext, "fake", True)

    def extract(content, path, selected):
        parser_languages.append(selected)
        return []

    monkeypatch.setattr(repo_map, "_load_language", load)
    monkeypatch.setattr(repo_map, "_extract_with_treesitter", extract)

    result = RepoMap(tmp_path).build()

    assert "a.py" in result and "b.py" in result
    assert loads == [(".py", False)]
    assert parser_languages == [language, language]


def test_refresh_forces_full_rediscovery_and_returns_report(tmp_path):
    (tmp_path / "first.py").write_text("def first(): pass\n")
    repo = RepoMap(tmp_path)
    initial = repo.build()
    assert "first.py" in initial
    (tmp_path / "second.py").write_text("def second(): pass\n")

    assert "second.py" not in repo.build()

    report = repo.refresh()
    refreshed = repo.build()

    assert report.force_refresh
    assert report.files_discovered == 2
    assert report.files_parsed == 2
    assert ".py" in report.languages
    assert "second.py" in refreshed
    assert repo.last_report is report


def test_nested_function_is_not_misclassified_as_method():
    code = """
def outer():
    def inner():
        pass

class Service:
    def run(self):
        pass
"""
    symbols = repo_map._extract_python_symbols(code, Path("sample.py"))
    by_name = {symbol.name: symbol for symbol in symbols}

    assert by_name["inner"].indent > 0
    assert by_name["inner"].kind == "function"
    assert by_name["run"].kind == "method"


def test_import_reference_and_path_signals_affect_importance():
    central = FileInfo(
        path=Path("src/core.py"),
        size=2_000,
        symbols=[Symbol("Engine", "class", 1, Path("src/core.py"))],
        import_count=4,
        reference_count=8,
        path_score=1.7,
    )
    peripheral = FileInfo(
        path=Path("tests/helper.py"),
        size=2_000,
        symbols=[Symbol("Helper", "class", 1, Path("tests/helper.py"))],
    )

    assert central.importance_score() > peripheral.importance_score()


def test_agent_exposes_selective_repo_map_cache_invalidation(tmp_path):
    agent = Agent(MockBackend([]), ToolRegistry())
    agent._repo_map_cache_key = str(tmp_path)
    agent._repo_map_cache = "cached"

    assert not agent.invalidate_repo_map_cache(tmp_path / "other")
    assert agent._repo_map_cache == "cached"
    assert agent.invalidate_repo_map_cache(tmp_path)
    assert not hasattr(agent, "_repo_map_cache")
    assert not agent.invalidate_repo_map_cache(tmp_path)

def test_agent_invalidation_forces_refresh_on_live_repo_map(tmp_path):
    (tmp_path / "first.py").write_text("def first(): pass\n")
    agent = Agent(MockBackend([]), ToolRegistry())
    agent._current_repo_path = str(tmp_path)
    agent._repo_map_cache_key = str(tmp_path)
    history = ConversationHistory()
    history.add(LLMMessage("user", "task"))
    budget = TokenBudget()
    live_map = RepoMap(tmp_path)

    agent._build_messages(history, budget, live_map)
    assert "first.py" in agent._repo_map_cache
    (tmp_path / "second.py").write_text("def second(): pass\n")

    assert agent.invalidate_repo_map_cache(tmp_path)
    agent._build_messages(history, budget, live_map)

    assert "second.py" in agent._repo_map_cache
    assert live_map.last_report is not None
    assert live_map.last_report.force_refresh