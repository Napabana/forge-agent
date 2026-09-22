"""Offline aggregator for frozen Forge Agent Real-model Benchmark V1 artifacts.

This module never constructs an LLM backend. It only reads the frozen suite and two
completed evaluation artifact directories, validates A/B fairness, and writes summary
artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SUITE = _ROOT / "evals" / "fixtures" / "coding_agent" / "benchmark_v1.json"
BASELINE_VARIANT = "baseline_react"
FULL_VARIANT = "planning_recovery_skills"
EXPECTED_TASK_COUNT = 12
EXPECTED_REPETITIONS = 2
EXPECTED_TRIALS_PER_VARIANT = 24


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{number}")
        rows.append(value)
    return rows


def _suite_identity(path: Path) -> tuple[dict[str, Any], str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("benchmark suite must be a JSON object")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return value, hashlib.sha256(canonical).hexdigest()


def _run_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    value = metadata.get("run_metadata") or {}
    if not isinstance(value, dict):
        raise ValueError("metadata.run_metadata must be an object")
    return value


def _load_artifact(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_dir():
        raise FileNotFoundError(f"artifact directory not found: {path}")
    return _read_json(path / "metadata.json"), _read_jsonl(path / "raw.jsonl")


def _validate_artifact(
    *,
    label: str,
    expected_variant: str,
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    suite_id: str,
    suite_sha256: str,
    task_ids: set[str],
    source_repository: str,
    source_commit: str,
) -> None:
    if metadata.get("execution_status") != "executed" or metadata.get("real_model_executed") is not True:
        raise ValueError(f"{label} is not an executed real-model artifact")
    if metadata.get("variant") != expected_variant:
        raise ValueError(f"{label} variant mismatch: {metadata.get('variant')!r}")
    if metadata.get("suite_id") != suite_id:
        raise ValueError(f"{label} suite_id mismatch")
    if int(metadata.get("repetitions", 0)) != EXPECTED_REPETITIONS:
        raise ValueError(f"{label} repetitions must equal {EXPECTED_REPETITIONS}")
    if int(metadata.get("task_count", 0)) != EXPECTED_TASK_COUNT:
        raise ValueError(f"{label} task_count must equal {EXPECTED_TASK_COUNT}")
    if len(rows) != EXPECTED_TRIALS_PER_VARIANT:
        raise ValueError(f"{label} must contain exactly {EXPECTED_TRIALS_PER_VARIANT} trials; got {len(rows)}")
    run_meta = _run_metadata(metadata)
    if run_meta.get("suite_sha256") != suite_sha256:
        raise ValueError(f"{label} suite_sha256 does not match frozen suite")
    if run_meta.get("source_repository") != source_repository:
        raise ValueError(f"{label} source_repository does not match frozen suite")
    if run_meta.get("source_commit") != source_commit:
        raise ValueError(f"{label} source_commit does not match frozen suite")
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if row.get("variant") != expected_variant:
            raise ValueError(f"{label} raw row variant mismatch")
        if row.get("suite_id") != suite_id:
            raise ValueError(f"{label} raw row suite_id mismatch")
        if row.get("real_model_executed") is not True or row.get("evidence_kind") != "real_model":
            raise ValueError(f"{label} contains non-real-model row")
        task_id = str(row.get("task_id") or "")
        repetition = int(row.get("repetition", 0))
        if task_id not in task_ids or repetition not in {1, 2}:
            raise ValueError(f"{label} contains unexpected trial identity: {task_id}/r{repetition}")
        key = (task_id, repetition)
        if key in seen:
            raise ValueError(f"{label} contains duplicate trial: {key}")
        seen.add(key)
    expected = {(task_id, rep) for task_id in task_ids for rep in (1, 2)}
    if seen != expected:
        raise ValueError(f"{label} is missing frozen trials: {sorted(expected - seen)}")


def _validate_fairness(baseline_meta: dict[str, Any], full_meta: dict[str, Any]) -> dict[str, Any]:
    left, right = _run_metadata(baseline_meta), _run_metadata(full_meta)
    same_fields = (
        "provider",
        "protocol",
        "model",
        "suite_sha256",
        "source_repository",
        "source_commit",
        "max_steps",
        "budget_tokens",
        "repo_map_mode",
        "mcp_enabled",
        "mcp_server_ids",
    )
    for field in same_fields:
        if left.get(field) != right.get(field):
            raise ValueError(f"fairness violation: run_metadata.{field} differs between variants")
    expected = {
        BASELINE_VARIANT: {"planning_mode": "off", "recovery_mode": "off", "skills_enabled": False},
        FULL_VARIANT: {"planning_mode": "always", "recovery_mode": "structured", "skills_enabled": True},
    }
    for label, meta, variant in (
        ("baseline", left, BASELINE_VARIANT),
        ("full_p2", right, FULL_VARIANT),
    ):
        for field, value in expected[variant].items():
            if meta.get(field) != value:
                raise ValueError(
                    f"{label} architecture mismatch: {field}={meta.get(field)!r}, expected={value!r}"
                )
        if meta.get("mcp_enabled") is not False:
            raise ValueError(f"{label} must keep MCP disabled for Benchmark V1")
    return {field: left.get(field) for field in same_fields}


def _metric(row: dict[str, Any], name: str) -> float:
    return float((row.get("metrics") or {}).get(name, 0) or 0)


def _distribution(rows: list[dict[str, Any]], metric: str) -> dict[str, float | int]:
    values = [_metric(row, metric) for row in rows]
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else 0.0,
        "median": statistics.median(values) if values else 0.0,
    }


def _rate(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    successes = sum(bool(row.get("success")) for row in rows)
    return {
        "trials": len(rows),
        "successes": successes,
        "observed_success_rate": successes / len(rows) if rows else 0.0,
    }


def _skill_process_match(row: dict[str, Any]) -> bool:
    checks = [g for g in row.get("grader_results", []) if g.get("kind") == "skill_selection"]
    return bool(checks) and all(bool(g.get("passed")) for g in checks)


def _variant_summary(rows: list[dict[str, Any]], tags: dict[str, set[str]]) -> dict[str, Any]:
    successful = [row for row in rows if row.get("success")]
    recovery = [row for row in rows if "recovery" in tags[str(row["task_id"])]]
    trigger = [row for row in rows if "skill-should-trigger" in tags[str(row["task_id"])]]
    no_trigger = [row for row in rows if "skill-should-not-trigger" in tags[str(row["task_id"])]]
    selected = sum(_metric(row, "skill_selected_count") > 0 for row in trigger)
    loaded = sum(_metric(row, "skill_loaded_count") > 0 for row in trigger)
    false_trigger = sum(_metric(row, "skill_loaded_count") > 0 for row in no_trigger)
    return {
        **_rate(rows),
        "successful_only": {
            "steps": _distribution(successful, "steps"),
            "total_tokens": _distribution(successful, "total_tokens"),
            "wall_time_seconds": _distribution(successful, "wall_time_seconds"),
        },
        "all_trials_audit": {
            "steps": _distribution(rows, "steps"),
            "total_tokens": _distribution(rows, "total_tokens"),
            "wall_time_seconds": _distribution(rows, "wall_time_seconds"),
        },
        "recovery_tasks": _rate(recovery),
        "skills": {
            "should_trigger_trials": len(trigger),
            "selected_trials": selected,
            "selected_rate": selected / len(trigger) if trigger else 0.0,
            "loaded_trials": loaded,
            "loaded_rate": loaded / len(trigger) if trigger else 0.0,
            "selection_process_match_trials": sum(_skill_process_match(row) for row in trigger),
            "selection_process_match_rate": (
                sum(_skill_process_match(row) for row in trigger) / len(trigger) if trigger else 0.0
            ),
            "should_not_trigger_trials": len(no_trigger),
            "false_trigger_trials": false_trigger,
            "false_trigger_rate": false_trigger / len(no_trigger) if no_trigger else 0.0,
        },
        "process_audit": {
            "completion_rejections": _distribution(rows, "completion_rejection_count"),
            "plan_revisions": _distribution(rows, "plan_revision_count"),
            "recoveries_selected": _distribution(rows, "recovery_selected_count"),
            "test_attempts": _distribution(rows, "test_attempt_count"),
        },
    }


def build_summary(*, suite_path: Path, baseline_dir: Path, full_dir: Path) -> dict[str, Any]:
    suite, suite_sha256 = _suite_identity(suite_path)
    tasks = list(suite.get("tasks") or [])
    if len(tasks) != EXPECTED_TASK_COUNT:
        raise ValueError(f"Benchmark V1 suite must contain {EXPECTED_TASK_COUNT} tasks")
    suite_id = str(suite.get("suite_id") or "")
    source = suite.get("source") or {}
    if not isinstance(source, dict):
        raise ValueError("Benchmark V1 source must be an object")
    source_repository = str(source.get("repository") or "")
    source_commit = str(source.get("commit") or "")
    if not source_repository or not source_commit:
        raise ValueError("Benchmark V1 requires frozen source repository and commit")
    tags = {str(task["id"]): set(task.get("tags") or []) for task in tasks}
    task_ids = set(tags)
    baseline_meta, baseline_rows = _load_artifact(baseline_dir)
    full_meta, full_rows = _load_artifact(full_dir)
    _validate_artifact(
        label="baseline",
        expected_variant=BASELINE_VARIANT,
        metadata=baseline_meta,
        rows=baseline_rows,
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        task_ids=task_ids,
        source_repository=source_repository,
        source_commit=source_commit,
    )
    _validate_artifact(
        label="full_p2",
        expected_variant=FULL_VARIANT,
        metadata=full_meta,
        rows=full_rows,
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        task_ids=task_ids,
        source_repository=source_repository,
        source_commit=source_commit,
    )
    common = _validate_fairness(baseline_meta, full_meta)
    baseline = _variant_summary(baseline_rows, tags)
    full = _variant_summary(full_rows, tags)

    left = {(str(row["task_id"]), int(row["repetition"])): row for row in baseline_rows}
    right = {(str(row["task_id"]), int(row["repetition"])): row for row in full_rows}
    wins = losses = ties = 0
    for key in sorted(left):
        base_ok, full_ok = bool(left[key].get("success")), bool(right[key].get("success"))
        if full_ok and not base_ok:
            wins += 1
        elif base_ok and not full_ok:
            losses += 1
        else:
            ties += 1

    per_task = {}
    for task in tasks:
        task_id = str(task["id"])
        a = _rate([row for row in baseline_rows if row["task_id"] == task_id])
        b = _rate([row for row in full_rows if row["task_id"] == task_id])
        per_task[task_id] = {
            "tags": list(task.get("tags") or []),
            "baseline": a,
            "full_p2": b,
            "absolute_delta": b["observed_success_rate"] - a["observed_success_rate"],
        }

    delta = float(full["observed_success_rate"]) - float(baseline["observed_success_rate"])
    return {
        "schema_version": 1,
        "benchmark_id": "forge-agent-real-model-benchmark-v1",
        "suite_id": suite_id,
        "suite_path": str(suite_path),
        "suite_sha256": suite_sha256,
        "task_count": EXPECTED_TASK_COUNT,
        "repetitions": EXPECTED_REPETITIONS,
        "planned_trials": EXPECTED_TRIALS_PER_VARIANT * 2,
        "variants": [BASELINE_VARIANT, FULL_VARIANT],
        "common_run_config": common,
        "primary_metric": {
            "name": "Independent Acceptance Pass Rate",
            "baseline": {
                "observed_successes": baseline["successes"],
                "trials": baseline["trials"],
                "observed_success_rate": baseline["observed_success_rate"],
            },
            "full_p2": {
                "observed_successes": full["successes"],
                "trials": full["trials"],
                "observed_success_rate": full["observed_success_rate"],
            },
            "absolute_delta": delta,
            "absolute_delta_percentage_points": delta * 100.0,
        },
        "baseline": baseline,
        "full_p2": full,
        "paired_outcomes": {"full_p2_wins": wins, "full_p2_losses": losses, "ties": ties},
        "per_task": per_task,
        "claim_boundary": (
            "Observed outcomes from one frozen, project-authored 12-task suite with two repetitions "
            "per architecture variant. They are not a stable population pass@1 estimate, do not use "
            "an LLM judge, and should not be generalized beyond this benchmark without additional "
            "evaluation. Successful-only efficiency metrics exclude failed trials; all-trial audit "
            "metrics are retained separately."
        ),
    }


def _pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    primary = summary["primary_metric"]
    baseline, full = summary["baseline"], summary["full_p2"]
    lines = [
        "# Forge Agent Real-model Benchmark V1",
        "",
        f"Benchmark ID: {summary['benchmark_id']}",
        f"Suite: {summary['suite_id']}",
        f"Suite SHA-256: {summary['suite_sha256']}",
        f"Tasks x repetitions x variants: {summary['task_count']} x {summary['repetitions']} x 2 = {summary['planned_trials']}",
        f"Provider / model: {summary['common_run_config'].get('provider')} / {summary['common_run_config'].get('model')}",
        "",
        "## Primary metric",
        "",
        "| Variant | Observed successes | Trials | Observed success rate |",
        "| --- | ---: | ---: | ---: |",
        f"| baseline_react | {primary['baseline']['observed_successes']} | {primary['baseline']['trials']} | {_pct(primary['baseline']['observed_success_rate'])} |",
        f"| planning_recovery_skills | {primary['full_p2']['observed_successes']} | {primary['full_p2']['trials']} | {_pct(primary['full_p2']['observed_success_rate'])} |",
        "",
        f"Absolute delta: {primary['absolute_delta_percentage_points']:+.1f} percentage points.",
        "",
        "## Successful-trial efficiency",
        "",
        "| Variant | Steps mean / median | Tokens mean / median | Wall time mean / median (s) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, data in (("baseline_react", baseline), ("planning_recovery_skills", full)):
        success = data["successful_only"]
        lines.append(
            f"| {name} | {success['steps']['mean']:.2f} / {success['steps']['median']:.2f} | "
            f"{success['total_tokens']['mean']:.1f} / {success['total_tokens']['median']:.1f} | "
            f"{success['wall_time_seconds']['mean']:.2f} / {success['wall_time_seconds']['median']:.2f} |"
        )
    lines += [
        "",
        "## Recovery and Skills subgroups",
        "",
        "| Variant | Recovery success | Skill trigger loaded | Skill false-trigger |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, data in (("baseline_react", baseline), ("planning_recovery_skills", full)):
        recovery, skills = data["recovery_tasks"], data["skills"]
        lines.append(
            f"| {name} | {recovery['successes']}/{recovery['trials']} ({_pct(recovery['observed_success_rate'])}) | "
            f"{skills['loaded_trials']}/{skills['should_trigger_trials']} ({_pct(skills['loaded_rate'])}) | "
            f"{skills['false_trigger_trials']}/{skills['should_not_trigger_trials']} ({_pct(skills['false_trigger_rate'])}) |"
        )
    paired = summary["paired_outcomes"]
    lines += [
        "",
        "## Paired outcomes",
        "",
        f"Full-P2 wins: {paired['full_p2_wins']}, losses: {paired['full_p2_losses']}, ties: {paired['ties']}.",
        "",
        "## Per-task comparison",
        "",
        "| Task | Baseline | Full P2 | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for task_id, row in summary["per_task"].items():
        a, b = row["baseline"], row["full_p2"]
        lines.append(
            f"| {task_id} | {a['successes']}/{a['trials']} | {b['successes']}/{b['trials']} | "
            f"{100 * row['absolute_delta']:+.1f} pp |"
        )
    lines += ["", "## Claim boundary", "", summary["claim_boundary"], ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--full-p2", required=True)
    parser.add_argument("--suite", default=str(_DEFAULT_SUITE))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output = Path(args.output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"benchmark summary output already exists; refusing to overwrite: {output}")
    summary = build_summary(
        suite_path=Path(args.suite).resolve(),
        baseline_dir=Path(args.baseline).resolve(),
        full_dir=Path(args.full_p2).resolve(),
    )
    output.mkdir(parents=True)
    (output / "benchmark_v1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "benchmark_v1_summary.md").write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({
        "summary_json": str(output / "benchmark_v1_summary.json"),
        "summary_markdown": str(output / "benchmark_v1_summary.md"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
