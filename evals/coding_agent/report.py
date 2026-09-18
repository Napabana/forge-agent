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
            }
        real_model_small_sample = aggregate
        claim_boundary += " Real-model rates are observed small-sample values, not stable pass@1 estimates."

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
