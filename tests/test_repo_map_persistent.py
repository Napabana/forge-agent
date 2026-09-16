from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from context.incremental_repo_map import PersistentRepoMap
from context.repo_index import SCHEMA_VERSION
from context.repository_state import capture_repository_state, detect_repository_changes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_git(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "repo-map@test.invalid")
    _git(repo, "config", "user.name", "Repo Map Test")


def _commit_all(repo: Path, message: str = "snapshot") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _symbols(repo_map: PersistentRepoMap, path: str) -> set[str]:
    files = {item.path.as_posix(): item for item in repo_map._files or []}
    return {symbol.name for symbol in files[path].symbols}


def test_full_build_then_second_instance_reuses_persistent_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    index = tmp_path / "cache" / "index.sqlite3"

    first = PersistentRepoMap(repo, index_path=index)
    first_text = first.build(query="alpha")
    assert index.exists()
    assert first.metrics.last_operation == "full_rebuild"
    assert first.last_report is not None and first.last_report.files_parsed == 1

    second = PersistentRepoMap(repo, index_path=index)
    second_text = second.build(query="alpha")
    assert second_text == first_text
    assert second.metrics.last_operation == "warm_load"
    assert second.last_report is not None and second.last_report.files_parsed == 0
    assert second.metrics.parsed_paths == []


def test_query_change_only_reranks_without_reparse(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "alpha.py").write_text("def alpha_handler():\n    return 1\n")
    (repo / "beta.py").write_text("def beta_handler():\n    return 2\n")
    repo_map = PersistentRepoMap(repo, index_path=tmp_path / "index.sqlite3")

    alpha = repo_map.build(query="alpha handler")
    parsed_after_build = list(repo_map.metrics.parsed_paths)
    beta = repo_map.build(query="beta handler")

    assert alpha != beta
    assert repo_map.metrics.parsed_paths == parsed_after_build
    assert repo_map.metrics.last_operation == "full_rebuild"


def test_single_file_modify_add_delete_and_rename_are_incremental(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    (repo / "b.py").write_text("def beta():\n    return 2\n")
    _commit_all(repo)
    repo_map = PersistentRepoMap(repo, index_path=tmp_path / "index.sqlite3")
    repo_map.build()

    (repo / "a.py").write_text("def alpha_new():\n    return 3\n")
    repo_map.sync()
    assert repo_map.metrics.parsed_paths[-1:] == ["a.py"]
    assert _symbols(repo_map, "a.py") == {"alpha_new"}

    (repo / "c.py").write_text("def gamma():\n    return 4\n")
    repo_map.sync()
    assert repo_map.metrics.parsed_paths[-1:] == ["c.py"]

    (repo / "b.py").unlink()
    repo_map.sync()
    assert "b.py" not in {item.path.as_posix() for item in repo_map._files or []}

    _git(repo, "add", "-A")
    _git(repo, "mv", "a.py", "renamed.py")
    repo_map.sync()
    paths = {item.path.as_posix() for item in repo_map._files or []}
    assert "a.py" not in paths
    assert "renamed.py" in paths
    assert _symbols(repo_map, "renamed.py") == {"alpha_new"}


def test_staged_unstaged_untracked_and_head_changes_are_detected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    (repo / "staged.py").write_text("def old_staged(): pass\n")
    (repo / "unstaged.py").write_text("def old_unstaged(): pass\n")
    _commit_all(repo)
    repo_map = PersistentRepoMap(repo, index_path=tmp_path / "index.sqlite3")
    repo_map.build()

    (repo / "staged.py").write_text("def new_staged(): pass\n")
    _git(repo, "add", "staged.py")
    (repo / "unstaged.py").write_text("def new_unstaged(): pass\n")
    (repo / "untracked.py").write_text("def new_untracked(): pass\n")
    repo_map.sync()

    assert _symbols(repo_map, "staged.py") == {"new_staged"}
    assert _symbols(repo_map, "unstaged.py") == {"new_unstaged"}
    assert _symbols(repo_map, "untracked.py") == {"new_untracked"}
    assert set(repo_map.metrics.parsed_paths[-3:]) == {"staged.py", "unstaged.py", "untracked.py"}

    _commit_all(repo, "head change")
    (repo / "head_only.py").write_text("def head_only(): pass\n")
    _commit_all(repo, "second head")
    rebuilds = len(repo_map.metrics.full_rebuild_seconds)
    repo_map.sync()
    assert "head_only.py" in {item.path.as_posix() for item in repo_map._files or []}
    assert len(repo_map.metrics.full_rebuild_seconds) == rebuilds


def test_non_git_change_falls_back_to_safe_full_rebuild(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha(): pass\n")
    repo_map = PersistentRepoMap(repo, index_path=tmp_path / "index.sqlite3")
    repo_map.build()
    (repo / "a.py").write_text("def beta(): pass\n")

    repo_map.sync()

    assert _symbols(repo_map, "a.py") == {"beta"}
    assert len(repo_map.metrics.full_rebuild_seconds) == 2
    assert "non_git_repository_changed" in repo_map.metrics.fallback_reasons


def test_schema_version_mismatch_and_corruption_rebuild_safely(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha(): pass\n")
    index = tmp_path / "index.sqlite3"
    first = PersistentRepoMap(repo, index_path=index)
    first.build()
    first._index.close()

    with sqlite3.connect(index) as conn:
        conn.execute("UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    mismatched = PersistentRepoMap(repo, index_path=index)
    mismatched.build()
    assert "schema_version_mismatch" in mismatched.metrics.fallback_reasons
    mismatched._index.close()

    index.write_bytes(b"not a sqlite database")
    corrupted = PersistentRepoMap(repo, index_path=index)
    assert "a.py" in corrupted.build()
    assert any(reason.startswith("corrupted_or_unusable") for reason in corrupted.metrics.fallback_reasons)


def test_reference_graph_and_old_symbol_cleanup_after_incremental_edits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    (repo / "defs.py").write_text("def alpha():\n    return 1\n")
    (repo / "use.py").write_text("from defs import alpha\nalpha()\nalpha()\n")
    _commit_all(repo)
    repo_map = PersistentRepoMap(repo, index_path=tmp_path / "index.sqlite3")
    repo_map.build()
    files = {item.path.as_posix(): item for item in repo_map._files or []}
    assert files["defs.py"].reference_count == 3

    (repo / "defs.py").write_text("def beta():\n    return 1\n")
    repo_map.sync()
    assert _symbols(repo_map, "defs.py") == {"beta"}
    files = {item.path.as_posix(): item for item in repo_map._files or []}
    assert files["defs.py"].reference_count == 0

    (repo / "use.py").write_text("from defs import beta\nbeta()\nbeta()\n")
    repo_map.sync()
    files = {item.path.as_posix(): item for item in repo_map._files or []}
    assert files["defs.py"].reference_count == 3


def test_dirty_file_content_change_updates_fingerprint_even_when_status_code_is_same(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    (repo / "a.py").write_text("value = 1\n")
    _commit_all(repo)
    (repo / "a.py").write_text("value = 2\n")
    first = capture_repository_state(repo)
    (repo / "a.py").write_text("value = 3\n")
    second = capture_repository_state(repo)
    assert first.status_paths == second.status_paths == ("a.py",)
    assert first.fingerprint != second.fingerprint
    assert detect_repository_changes(repo, first, second).all_paths == ("a.py",)


def test_default_index_is_outside_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("FORGE_AGENT_CACHE_DIR", str(tmp_path / "cache-root"))
    repo_map = PersistentRepoMap(repo)
    assert repo not in repo_map.index_path.parents
    assert repo_map.index_path.parent.parent == tmp_path / "cache-root" / "forge-agent"
