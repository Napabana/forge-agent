from pathlib import Path

from context.incremental_repo_map import PersistentRepoMap
from context.repo_map import RepoMap


def test_duplicate_definition_reference_semantics_match_legacy_repo_map(tmp_path):
    (tmp_path / "a.py").write_text(
        "def shared():\n"
        "    return 1\n\n"
        "def call_local():\n"
        "    return shared()\n"
    )
    (tmp_path / "b.py").write_text(
        "def shared():\n"
        "    return 2\n"
    )
    (tmp_path / "c.py").write_text(
        "def external():\n"
        "    return shared()\n"
    )

    legacy = RepoMap(tmp_path)
    legacy_files, _ = legacy._scan()
    legacy_counts = {item.rel_path: item.reference_count for item in legacy_files}

    persistent = PersistentRepoMap(
        tmp_path,
        index_path=tmp_path.parent / "reference-semantics.sqlite3",
    )
    persistent.build(query="shared")
    persistent_counts = {
        item.rel_path: item.reference_count for item in (persistent._files or ())
    }

    assert persistent_counts == legacy_counts
    assert legacy_counts["a.py"] == 1
    assert legacy_counts["b.py"] == 1
