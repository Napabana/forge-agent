from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.check_docs import (
    ChangedPath,
    analyze_changes,
    collect_git_changes,
    load_mapping,
    validate_links,
    validate_mapping,
    validate_source_references,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _mapping() -> dict:
    return load_mapping(REPO_ROOT)


def _changes(*paths: str) -> list[ChangedPath]:
    return [ChangedPath("M", path) for path in paths]


def test_single_module_change_maps_to_context_topic() -> None:
    result = analyze_changes(_changes("context/repo_map.py"), _mapping())

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["affected_modules"] == ["Context Engineering"]
    assert "docs/design/03-Context与RepoMap.md" in result["related_documents"]


def test_core_change_maps_to_multiple_topics() -> None:
    result = analyze_changes(_changes("agent/core.py"), _mapping())

    assert result["status"] == "REVIEW_REQUIRED"
    assert {
        "Context Engineering",
        "Planning & Recovery",
        "Skills & MCP",
        "Tool Lifecycle & Security",
    } <= set(result["affected_modules"])


def test_new_unmapped_source_is_reported() -> None:
    result = analyze_changes(_changes("agent/new_runtime.py"), _mapping())

    assert result["status"] == "UNMAPPED"
    assert result["unmapped_files"] == ["agent/new_runtime.py"]


def test_test_only_change_has_no_architecture_impact() -> None:
    result = analyze_changes(_changes("tests/test_runner.py"), _mapping())

    assert result["status"] == "NO_IMPACT"
    assert result["affected_modules"] == []


def test_no_changes_has_no_impact() -> None:
    assert analyze_changes([], _mapping())["status"] == "NO_IMPACT"


def test_related_topic_and_index_in_diff_report_docs_updated() -> None:
    result = analyze_changes(
        _changes(
            "context/repo_map.py",
            "docs/design/03-Context与RepoMap.md",
            "docs/design/源码索引.md",
        ),
        _mapping(),
    )

    assert result["status"] == "DOCS_UPDATED"


def test_missing_markdown_link_is_invalid(tmp_path: Path) -> None:
    page = tmp_path / "docs" / "page.md"
    page.parent.mkdir()
    page.write_text("[missing](other.md)\n", encoding="utf-8")

    errors = validate_links(tmp_path, ["docs/page.md"])

    assert errors == ["INVALID_REFERENCE docs/page.md -> other.md"]


def test_missing_source_reference_is_invalid(tmp_path: Path) -> None:
    page = tmp_path / "docs" / "page.md"
    page.parent.mkdir()
    page.write_text("See `agent/missing.py`.\n", encoding="utf-8")

    errors = validate_source_references(tmp_path, ["docs/page.md"])

    assert errors == ["INVALID_REFERENCE docs/page.md -> agent/missing.py"]


def test_renamed_symbol_is_invalid(tmp_path: Path) -> None:
    source = tmp_path / "agent" / "core.py"
    document = tmp_path / "docs" / "design.md"
    source.parent.mkdir()
    document.parent.mkdir()
    source.write_text("class Agent:\n    def renamed(self):\n        pass\n", encoding="utf-8")
    document.write_text("# Design\n", encoding="utf-8")
    mapping = {
        "modules": [
            {
                "document": "docs/design.md",
                "symbols": [{"path": "agent/core.py", "name": "Agent.run"}],
            }
        ]
    }

    errors = validate_mapping(tmp_path, mapping)

    assert errors == ["INVALID_REFERENCE missing symbol: agent/core.py::Agent.run"]


def test_git_collection_includes_rename_as_delete_add_and_untracked(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init")
    git("config", "user.email", "docs@example.invalid")
    git("config", "user.name", "Docs Test")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    (agent / "deleted.py").write_text("VALUE = 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "baseline")

    git("mv", "agent/old.py", "agent/renamed.py")
    (agent / "deleted.py").unlink()
    (agent / "new.py").write_text("VALUE = 3\n", encoding="utf-8")

    changes = collect_git_changes(tmp_path)
    by_path = {change.path: change for change in changes}

    assert by_path["agent/old.py"].status == "D"
    assert by_path["agent/renamed.py"].status == "A"
    assert by_path["agent/deleted.py"].status == "D"
    assert by_path["agent/new.py"].status == "A"


def test_architecture_map_is_valid_json() -> None:
    path = REPO_ROOT / "docs/design/architecture-map.json"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
