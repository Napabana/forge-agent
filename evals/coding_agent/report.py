"""Aggregation and artifact serialization for Coding Agent evaluations."""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from evals.coding_agent.schema import EvalReport, TrialResult


def _mean(values: list[float | int]) -> float:
    return statistics.mean(values) if values else 0.0


def build_report(results: Iterable[TrialResult], *, suite_id: str) -> dict[str, Any]:
    rows = list(results)
    real_rows = [row for row in rows if row.real_model_executed and row.evidence_kind == "real_model"]
    harness_rows = [row for row in rows if row not in real_rows]
    variants = tuple(sorted({row.variant for row in rows}))
    claim_boundary = (
        "Fake/scripted trials validate the Evaluation Harness only. Capability metrics are emitted "
        "only for explicitly executed real-model trials."
    )
    harness_validation = None
    if harness_rows:
        harness_validation = {
            "trial_count": len(harness_rows),
            "passing_trials": sum(row.success for row in harness_rows),
            "pass_rate_intentionally_omitted": True,
        }

    real_model_small_sample = None
    if real_rows:
        aggregate: dict[str, Any] = {}
        for variant in sorted({row.variant for row in real_rows}):
            group = [row for row in real_rows if row.variant == variant]
            skill_selection_checks = [
                grader
                for row in group
                for grader in row.grader_results
                if grader.kind == "skill_selection"
            ]
            aggregate[variant] = {
                "runs": len(group),
                "observed_successes": sum(row.success for row in group),
                "observed_success_rate": sum(row.success for row in group) / len(group),
                "mean_steps": _mean([row.metrics.steps for row in group]),
                "mean_total_tokens": _mean([row.metrics.total_tokens for row in group]),
                "mean_wall_time_seconds": _mean([row.metrics.wall_time_seconds for row in group]),
                "mean_tool_calls": _mean([row.metrics.tool_call_count for row in group]),
                "mean_test_attempts": _mean([row.metrics.test_attempt_count for row in group]),
                "mean_completion_rejections": _mean([
                    row.metrics.completion_rejection_count for row in group
                ]),
                "mean_plan_created": _mean([row.metrics.plan_created_count for row in group]),
                "mean_plan_revisions": _mean([row.metrics.plan_revision_count for row in group]),
                "mean_plan_steps_completed": _mean([
                    row.metrics.plan_step_completed_count for row in group
                ]),
                "planning_skipped_runs": sum(row.metrics.planning_skipped for row in group),
                "mean_failures_classified": _mean([
                    row.metrics.failure_classified_count for row in group
                ]),
                "mean_recoveries_selected": _mean([
                    row.metrics.recovery_selected_count for row in group
                ]),
                "mean_replans_selected": _mean([
                    row.metrics.recovery_replan_count for row in group
                ]),
                "recovery_exhausted_runs": sum(
                    row.metrics.recovery_exhausted_count > 0 for row in group
                ),
                "mean_skills_discovered": _mean([
                    row.metrics.skill_discovered_count for row in group
                ]),
                "mean_skills_selected": _mean([
                    row.metrics.skill_selected_count for row in group
                ]),
                "mean_skills_loaded": _mean([
                    row.metrics.skill_loaded_count for row in group
                ]),
                "mean_skill_references_loaded": _mean([
                    row.metrics.skill_reference_loaded_count for row in group
                ]),
                "mean_mcp_tools_discovered": _mean([
                    row.metrics.mcp_tool_discovered_count for row in group
                ]),
                "mean_mcp_tool_calls": _mean([
                    row.metrics.mcp_tool_call_count for row in group
                ]),
                "mean_mcp_tool_failures": _mean([
                    row.metrics.mcp_tool_failure_count for row in group
                ]),
                "skill_selection_process_checks": len(skill_selection_checks),
                "skill_selection_process_passes": sum(
                    grader.passed for grader in skill_selection_checks
                ),
            }
        real_model_small_sample = aggregate
        claim_boundary += (
            " Real-model rates are observed small-sample values, not stable pass@1 estimates."
            " Skill-selection checks are non-blocking process evidence and do not change task success."
        )

    return EvalReport(
        suite_id=suite_id,
        execution_status="executed" if rows else "not_executed",
        real_model_executed=bool(real_rows),
        trial_count=len(rows),
        variants=variants,
        claim_boundary=claim_boundary,
        harness_validation=harness_validation,
        real_model_small_sample=real_model_small_sample,
    ).to_dict()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_report_artifacts(output_dir: Path, results: list[TrialResult], *, suite_id: str) -> dict[str, Any]:
    report = build_report(results, suite_id=suite_id)
    write_json(output_dir / "report.json", report)
    with (output_dir / "raw.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    return report
