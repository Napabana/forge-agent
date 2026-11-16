from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.context_policy_benchmark import (
    FixtureSemanticSummarizer,
    _evaluate_case_variant,
    load_manifest,
    run_benchmark,
)


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
def context_policy_smoke(tmp_path_factory: pytest.TempPathFactory):
    """pytest 只跑代表性 smoke；完整 7×3 benchmark 由 CLI 显式执行。"""
    manifest = load_manifest()
    cases = {case["id"]: case for case in manifest["cases"]}
    defaults = manifest["defaults"]
    root = Path(__file__).resolve().parents[1]
    tmp = tmp_path_factory.mktemp("context-policy-b1-smoke")

    # 代表性三 variant：huge-tool-output 同时覆盖 baseline / Stage A / Hybrid。
    smoke_manifest = {
        "schema_version": manifest["schema_version"],
        "defaults": defaults,
        "cases": [cases["huge-tool-output"]],
    }
    smoke_manifest_path = tmp / "manifest.json"
    smoke_manifest_path.write_text(
        json.dumps(smoke_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output = tmp / "results"
    report = run_benchmark(
        repo=root,
        manifest=smoke_manifest_path,
        output=output,
        semantic_mode="fixture",
        allow_dirty=True,
    )
    rows = [
        json.loads(line)
        for line in (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # 其余只挑 C5/C3 最关键 lifecycle 契约跑 Hybrid，避免把正式 benchmark 塞进 pytest。
    targeted: dict[str, dict] = {}
    for case_id in (
        "early-hard-constraint",
        "superseded-state",
        "repeated-compaction",
        "resume-long-session",
    ):
        targeted[case_id] = _evaluate_case_variant(
            case=cases[case_id],
            defaults=defaults,
            variant="hybrid_compaction",
            workspace=tmp / "targeted" / case_id,
            semantic_mode="fixture",
            live_backend=None,
        )

    return output, report, rows, targeted


def test_context_policy_manifest_has_frozen_cases() -> None:
    manifest = load_manifest()
    assert {case["id"] for case in manifest["cases"]} == EXPECTED_CASES
    assert manifest["defaults"]["budget_tokens"] == 4096
    assert manifest["defaults"]["threshold"] == 0.8
    assert manifest["defaults"]["keep_recent_tokens"] == 640


def test_context_policy_fixture_replay_contracts(context_policy_smoke) -> None:
    output, report, rows, targeted = context_policy_smoke
    assert len(rows) == 3
    assert report["rows"] == 3
    assert (output / "metadata.json").exists()
    assert (output / "report.json").exists()
    assert (output / "report.md").exists()

    assert all(row["canonical_unchanged"] for row in rows)
    assert all(row["orphan_actions"] == 0 for row in rows)
    assert all(row["orphan_observations"] == 0 for row in rows)
    assert all(row["recent_raw_recall"] == 1.0 for row in rows)
    assert all(row["raw_event_traceable"] for row in rows)
    assert all(row["source_event_coverage"] == 1.0 for row in rows)

    by_variant = {row["variant"]: row for row in rows}
    assert by_variant["deterministic_pruning"]["final_tokens"] < by_variant["budget_trim_only"]["raw_history_tokens"]
    assert by_variant["hybrid_compaction"]["final_tokens"] < by_variant["budget_trim_only"]["raw_history_tokens"]

    early = targeted["early-hard-constraint"]
    assert early["hard_constraint_recall"] == 1.0
    assert early["passed"] is True

    superseded = targeted["superseded-state"]
    assert superseded["latest_test_status"] == "PASS"
    assert superseded["stale_failure_violations"] == 0
    assert superseded["working_set_recall"] == 1.0
    assert superseded["passed"] is True

    repeated = targeted["repeated-compaction"]
    assert repeated["policy_calls"] == 3
    assert repeated["summary_call_count"] == 2
    assert repeated["active_view_reused"] is True
    assert repeated["lineage_correct"] is True
    assert repeated["checkpoint_atomic"] is True
    assert repeated["passed"] is True

    resumed = targeted["resume-long-session"]
    assert resumed["lineage_correct"] is True
    assert resumed["current_user_count"] == 1
    assert resumed["passed"] is True


def test_context_policy_report_contains_three_variants(context_policy_smoke) -> None:
    output, report, _, _ = context_policy_smoke
    assert set(report["aggregate"]) == {
        "budget_trim_only",
        "deterministic_pruning",
        "hybrid_compaction",
    }
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["semantic"]["mode"] == "fixture"
    assert metadata["case_count"] == 1
    assert len(metadata["fixture_sha256"]) == 64
