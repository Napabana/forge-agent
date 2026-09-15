from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.context_policy_benchmark import load_manifest, run_benchmark


EXPECTED_CASES = {
    "early-hard-constraint",
    "huge-tool-output",
    "action-observation-pair",
    "superseded-state",
    "repeated-compaction",
    "resume-long-session",
    "dirty-repo-revision",
}


@pytest.fixture(scope="module")
def context_policy_replay(tmp_path_factory: pytest.TempPathFactory):
    """完整 7×3 replay 在本测试模块只执行一次，后续断言共享结果。"""
    output = tmp_path_factory.mktemp("context-policy-b1") / "results"
    report = run_benchmark(
        repo=Path(__file__).resolve().parents[1],
        output=output,
        semantic_mode="fixture",
        allow_dirty=True,
    )
    rows = [
        json.loads(line)
        for line in (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return output, report, rows


def test_context_policy_manifest_has_frozen_cases() -> None:
    manifest = load_manifest()
    assert {case["id"] for case in manifest["cases"]} == EXPECTED_CASES
    assert manifest["defaults"]["budget_tokens"] == 4096
    assert manifest["defaults"]["threshold"] == 0.8
    assert manifest["defaults"]["keep_recent_tokens"] == 640


def test_context_policy_fixture_replay_contracts(context_policy_replay) -> None:
    output, report, rows = context_policy_replay
    assert len(rows) == len(EXPECTED_CASES) * 3
    assert report["rows"] == len(rows)
    assert (output / "metadata.json").exists()
    assert (output / "report.json").exists()
    assert (output / "report.md").exists()

    assert all(row["canonical_unchanged"] for row in rows)
    assert all(row["orphan_actions"] == 0 for row in rows)
    assert all(row["orphan_observations"] == 0 for row in rows)
    assert all(row["recent_raw_recall"] == 1.0 for row in rows)
    assert all(row["raw_event_traceable"] for row in rows)
    assert all(row["source_event_coverage"] == 1.0 for row in rows)

    by_key = {(row["case_id"], row["variant"]): row for row in rows}

    early = by_key[("early-hard-constraint", "hybrid_compaction")]
    assert early["hard_constraint_recall"] == 1.0
    assert early["passed"] is True

    huge_pruning = by_key[("huge-tool-output", "deterministic_pruning")]
    huge_hybrid = by_key[("huge-tool-output", "hybrid_compaction")]
    assert huge_pruning["final_tokens"] < by_key[("huge-tool-output", "budget_trim_only")]["raw_history_tokens"]
    assert huge_hybrid["final_tokens"] < by_key[("huge-tool-output", "budget_trim_only")]["raw_history_tokens"]

    superseded = by_key[("superseded-state", "hybrid_compaction")]
    assert superseded["latest_test_status"] == "PASS"
    assert superseded["stale_failure_violations"] == 0
    assert superseded["working_set_recall"] == 1.0
    assert superseded["passed"] is True

    repeated = by_key[("repeated-compaction", "hybrid_compaction")]
    assert repeated["policy_calls"] == 3
    assert repeated["summary_call_count"] == 2
    assert repeated["active_view_reused"] is True
    assert repeated["lineage_correct"] is True
    assert repeated["checkpoint_atomic"] is True
    assert repeated["passed"] is True

    resumed = by_key[("resume-long-session", "hybrid_compaction")]
    assert resumed["lineage_correct"] is True
    assert resumed["current_user_count"] == 1
    assert resumed["passed"] is True

    dirty = by_key[("dirty-repo-revision", "hybrid_compaction")]
    assert dirty["summary_call_count"] == 2
    assert dirty["repo_revision_changed"] is True
    assert dirty["lineage_correct"] is True
    assert dirty["passed"] is True


def test_context_policy_report_contains_three_variants(context_policy_replay) -> None:
    output, report, _ = context_policy_replay
    assert set(report["aggregate"]) == {
        "budget_trim_only",
        "deterministic_pruning",
        "hybrid_compaction",
    }
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["semantic"]["mode"] == "fixture"
    assert metadata["case_count"] == 7
    assert len(metadata["fixture_sha256"]) == 64
