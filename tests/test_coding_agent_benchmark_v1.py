from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.coding_agent.__main__ import (
    _mcp_config_for_variant,
    _planning_mode_for_variant,
    _recovery_mode_for_variant,
    _skills_enabled_for_variant,
)
from evals.coding_agent.runner import validate_suite_references, write_not_executed
from evals.coding_agent.schema import EvaluationSuite
from scripts.report_coding_agent_benchmark_v1 import (
    BASELINE_VARIANT,
    FULL_VARIANT,
    build_summary,
)

ROOT = Path(__file__).parents[1]
SUITE_PATH = ROOT / "evals" / "fixtures" / "coding_agent" / "benchmark_v1.json"
REQUIRED_TAGS = {
    "single-file-bug",
    "single-file-feature",
    "multi-file",
    "navigation",
    "recovery",
    "change-approach",
    "completion-guard",
    "replan-candidate",
    "skill-should-trigger",
    "skill-should-not-trigger",
}


def _suite_sha256() -> str:
    value = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_metadata(*, full: bool) -> dict[str, object]:
    return {
        "provider": "openai",
        "protocol": "chat_completions",
        "model": "test-model",
        "suite_sha256": _suite_sha256(),
        "max_steps": 20,
        "budget_tokens": 40000,
        "repo_map_mode": "incremental",
        "planning_mode": "always" if full else "off",
        "recovery_mode": "structured" if full else "off",
        "skills_enabled": full,
        "mcp_enabled": False,
        "mcp_server_ids": [],
    }


def _write_real_artifact(
    root: Path,
    suite: EvaluationSuite,
    *,
    variant: str,
    full: bool,
    failed: set[tuple[str, int]] = frozenset(),
) -> None:
    root.mkdir()
    metadata = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "variant": variant,
        "repetitions": 2,
        "task_count": len(suite.tasks),
        "planned_trial_count": len(suite.tasks) * 2,
        "execution_status": "executed",
        "real_model_executed": True,
        "run_metadata": _run_metadata(full=full),
    }
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    rows = []
    for repetition in (1, 2):
        for index, task in enumerate(suite.tasks):
            success = (task.task_id, repetition) not in failed
            should_trigger = "skill-should-trigger" in task.tags
            should_not_trigger = "skill-should-not-trigger" in task.tags
            loaded = bool(full and should_trigger)
            graders = []
            if should_trigger or should_not_trigger:
                graders.append({
                    "grader_id": "skill-selection",
                    "kind": "skill_selection",
                    "passed": loaded if should_trigger else not loaded,
                    "required": False,
                    "detail": "synthetic",
                    "evidence": {"selected_skills": ["synthetic"] if loaded else []},
                })
            rows.append({
                "suite_id": suite.suite_id,
                "task_id": task.task_id,
                "trial_id": f"{task.task_id}--{variant}--r{repetition:03d}",
                "variant": variant,
                "repetition": repetition,
                "execution_status": "executed",
                "evidence_kind": "real_model",
                "real_model_executed": True,
                "run_status": "success" if success else "gave_up",
                "termination_reason": "completion_satisfied" if success else "model_gave_up",
                "acceptance_status": "passed" if success else "not_run",
                "success": success,
                "metrics": {
                    "steps": 4 + index,
                    "total_tokens": 1000 + index * 100,
                    "wall_time_seconds": 2.0 + index / 10,
                    "test_attempt_count": 1 if "tests" in task.tags else 0,
                    "completion_rejection_count": 0,
                    "plan_revision_count": int(full and "replan-candidate" in task.tags),
                    "recovery_selected_count": int(full and "recovery" in task.tags),
                    "skill_selected_count": int(loaded),
                    "skill_loaded_count": int(loaded),
                },
                "grader_results": graders,
            })
    (root / "raw.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_benchmark_v1_suite_has_frozen_coverage_and_reference_solutions(tmp_path: Path):
    suite = EvaluationSuite.load(SUITE_PATH)
    assert suite.suite_id == "forge-agent-benchmark-v1"
    assert len(suite.tasks) == 12
    assert suite.defaults == {"max_steps": 20, "budget_tokens": 40000}
    tags = {tag for task in suite.tasks for tag in task.tags}
    assert REQUIRED_TAGS <= tags
    assert sum("skill-should-trigger" in task.tags for task in suite.tasks) >= 4
    assert sum("skill-should-not-trigger" in task.tags for task in suite.tasks) >= 2
    assert sum("recovery" in task.tags for task in suite.tasks) >= 3
    validated = validate_suite_references(suite, tmp_path / "references")
    assert len(validated) == 12
    assert all(validated[task.task_id] for task in suite.tasks)


def test_benchmark_v1_variant_mapping_keeps_mcp_out_of_main_ab():
    assert _planning_mode_for_variant(BASELINE_VARIANT) == "off"
    assert _recovery_mode_for_variant(BASELINE_VARIANT) == "off"
    assert _skills_enabled_for_variant(BASELINE_VARIANT) is False
    assert _mcp_config_for_variant(BASELINE_VARIANT).enabled is False

    assert _planning_mode_for_variant(FULL_VARIANT) == "always"
    assert _recovery_mode_for_variant(FULL_VARIANT) == "structured"
    assert _skills_enabled_for_variant(FULL_VARIANT) is True
    assert _mcp_config_for_variant(FULL_VARIANT).enabled is False


def test_benchmark_v1_dry_run_records_24_planned_trials_and_frozen_metadata(tmp_path: Path):
    suite = EvaluationSuite.load(SUITE_PATH)
    report = write_not_executed(
        suite,
        tmp_path / "dry",
        variant=FULL_VARIANT,
        repetitions=2,
        reason="benchmark_v1_freeze_check",
        run_metadata=_run_metadata(full=True),
    )
    metadata = json.loads((tmp_path / "dry" / "metadata.json").read_text(encoding="utf-8"))
    assert report["planned_trial_count"] == 24
    assert metadata["planned_trial_count"] == 24
    assert metadata["run_metadata"]["suite_sha256"] == _suite_sha256()
    assert metadata["run_metadata"]["max_steps"] == 20
    assert metadata["run_metadata"]["budget_tokens"] == 40000
    assert metadata["real_model_executed"] is False


def test_benchmark_v1_summary_uses_successful_only_metrics_and_complete_pairs(tmp_path: Path):
    suite = EvaluationSuite.load(SUITE_PATH)
    baseline_failed = {(suite.tasks[0].task_id, 1), (suite.tasks[1].task_id, 1)}
    full_failed = {(suite.tasks[1].task_id, 1)}
    baseline_dir, full_dir = tmp_path / "baseline", tmp_path / "full"
    _write_real_artifact(
        baseline_dir, suite, variant=BASELINE_VARIANT, full=False, failed=baseline_failed
    )
    _write_real_artifact(
        full_dir, suite, variant=FULL_VARIANT, full=True, failed=full_failed
    )
    summary = build_summary(
        suite_path=SUITE_PATH,
        baseline_dir=baseline_dir,
        full_dir=full_dir,
    )
    assert summary["planned_trials"] == 48
    assert summary["primary_metric"]["baseline"]["observed_successes"] == 22
    assert summary["primary_metric"]["full_p2"]["observed_successes"] == 23
    assert summary["paired_outcomes"] == {"full_p2_wins": 1, "full_p2_losses": 0, "ties": 23}
    assert summary["baseline"]["successful_only"]["steps"]["count"] == 22
    assert summary["baseline"]["all_trials_audit"]["steps"]["count"] == 24
    assert summary["full_p2"]["skills"]["false_trigger_rate"] == 0.0


def test_benchmark_v1_summary_rejects_budget_drift(tmp_path: Path):
    suite = EvaluationSuite.load(SUITE_PATH)
    baseline_dir, full_dir = tmp_path / "baseline", tmp_path / "full"
    _write_real_artifact(baseline_dir, suite, variant=BASELINE_VARIANT, full=False)
    _write_real_artifact(full_dir, suite, variant=FULL_VARIANT, full=True)
    metadata_path = full_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["run_metadata"]["budget_tokens"] = 50000
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="fairness violation"):
        build_summary(suite_path=SUITE_PATH, baseline_dir=baseline_dir, full_dir=full_dir)
