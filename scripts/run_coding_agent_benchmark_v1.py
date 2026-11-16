"""Run the formal Forge Agent Benchmark V1 R2 pipeline sequentially.

This launcher performs deterministic preflight first, then executes baseline_react
and planning_recovery_skills with the same frozen suite and source repository.
It never deletes or overwrites an existing artifact directory.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals" / "fixtures" / "coding_agent" / "benchmark_v1.json"
EXPECTED_SOURCE_COMMIT = "23019998f2e801e79dea59fd23fc49c58fc20038"
RESULTS = ROOT / "evals" / "results"

BASELINE_DRY = RESULTS / "benchmark_v1_r2_baseline_dry"
FULL_DRY = RESULTS / "benchmark_v1_r2_full_p2_dry"
BASELINE_REAL = RESULTS / "benchmark_v1_r2_baseline_real"
FULL_REAL = RESULTS / "benchmark_v1_r2_full_p2_real"
SUMMARY = RESULTS / "benchmark_v1_r2_summary"

INFRA_TERMINATIONS = {
    "provider_error",
    "evaluation_runner_exception",
    "evaluation_runner_cleanup_failure",
}


def run(command: list[str]) -> None:
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def ensure_outputs_absent() -> None:
    existing = [
        path
        for path in (BASELINE_DRY, FULL_DRY, BASELINE_REAL, FULL_REAL, SUMMARY)
        if path.exists()
    ]
    if existing:
        joined = "\n".join(f"  - {path}" for path in existing)
        raise SystemExit(
            "Benchmark V1 R2 refuses to overwrite existing artifacts. "
            "Inspect/remove or choose a new protocol revision manually:\n" + joined
        )


def verify_source(source_repo: Path) -> None:
    if not source_repo.is_dir():
        raise SystemExit(f"source repository not found: {source_repo}")
    completed = subprocess.run(
        ["git", "rev-parse", f"{EXPECTED_SOURCE_COMMIT}^{{commit}}"],
        cwd=source_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"frozen source commit is unavailable in {source_repo}: "
            f"{completed.stderr.strip()}"
        )
    resolved = completed.stdout.strip().lower()
    if resolved != EXPECTED_SOURCE_COMMIT:
        raise SystemExit(
            f"source commit mismatch: expected={EXPECTED_SOURCE_COMMIT}, resolved={resolved}"
        )


def check_real_artifact(path: Path, *, variant: str) -> None:
    metadata_path = path / "metadata.json"
    raw_path = path / "raw.jsonl"
    if not metadata_path.is_file() or not raw_path.is_file():
        raise SystemExit(f"incomplete real-model artifact: {path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("execution_status") != "executed":
        raise SystemExit(f"{variant}: execution_status is not executed")
    if metadata.get("real_model_executed") is not True:
        raise SystemExit(f"{variant}: real_model_executed is not true")
    if metadata.get("variant") != variant:
        raise SystemExit(
            f"{variant}: artifact variant mismatch: {metadata.get('variant')!r}"
        )
    if int(metadata.get("planned_trial_count", 0)) != 24:
        raise SystemExit(f"{variant}: expected 24 planned trials")

    rows = [
        json.loads(line)
        for line in raw_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != 24:
        raise SystemExit(f"{variant}: expected 24 completed rows, got {len(rows)}")

    infrastructure = []
    for row in rows:
        termination = str(row.get("termination_reason") or "")
        error = str(row.get("error") or "")
        if termination in INFRA_TERMINATIONS or error:
            infrastructure.append(
                {
                    "trial_id": row.get("trial_id"),
                    "termination_reason": termination,
                    "error": error,
                }
            )
    if infrastructure:
        print(json.dumps({"infrastructure_failures": infrastructure}, indent=2), flush=True)
        raise SystemExit(
            f"{variant}: infrastructure failure detected; stopping before the next variant"
        )

    successes = sum(bool(row.get("success")) for row in rows)
    print(
        json.dumps(
            {
                "variant": variant,
                "trials": len(rows),
                "observed_successes": successes,
                "observed_success_rate": successes / len(rows),
                "infrastructure_failures": 0,
            },
            indent=2,
        ),
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-repo",
        default=os.environ.get("SOURCE_REPO"),
        help="Local pr-test clone containing the frozen source commit; defaults to SOURCE_REPO.",
    )
    args = parser.parse_args()
    if not args.source_repo:
        raise SystemExit("--source-repo or SOURCE_REPO is required")

    source_repo = Path(args.source_repo).expanduser().resolve()
    verify_source(source_repo)
    ensure_outputs_absent()

    python = sys.executable
    source = str(source_repo)
    suite = str(SUITE.relative_to(ROOT))

    print(
        json.dumps(
            {
                "benchmark_id": "forge-agent-real-model-benchmark-v1-r2",
                "suite": suite,
                "source_repository": "Napabana/pr-test",
                "source_commit": EXPECTED_SOURCE_COMMIT,
                "source_repo_path": source,
                "task_count": 12,
                "repetitions": 2,
                "variants": ["baseline_react", "planning_recovery_skills"],
                "planned_real_model_trials": 48,
                "max_steps": 30,
                "budget_tokens": 60000,
            },
            indent=2,
        ),
        flush=True,
    )

    # Deterministic preflight. No Provider backend is constructed here.
    run([
        python,
        "-m",
        "pytest",
        "-q",
        "tests/test_coding_agent_eval.py",
        "tests/test_coding_agent_benchmark_v1.py",
    ])

    common = [
        python,
        "-m",
        "evals.coding_agent",
        "--suite",
        suite,
        "--source-repo",
        source,
        "--repetitions",
        "2",
    ]
    run(common + [
        "--variant",
        "baseline_react",
        "--output-dir",
        str(BASELINE_DRY.relative_to(ROOT)),
    ])
    run(common + [
        "--variant",
        "planning_recovery_skills",
        "--output-dir",
        str(FULL_DRY.relative_to(ROOT)),
    ])

    # Provider calls begin only after every deterministic preflight above passes.
    run(common + [
        "--variant",
        "baseline_react",
        "--output-dir",
        str(BASELINE_REAL.relative_to(ROOT)),
        "--real-model",
    ])
    check_real_artifact(BASELINE_REAL, variant="baseline_react")

    run(common + [
        "--variant",
        "planning_recovery_skills",
        "--output-dir",
        str(FULL_REAL.relative_to(ROOT)),
        "--real-model",
    ])
    check_real_artifact(FULL_REAL, variant="planning_recovery_skills")

    run([
        python,
        "scripts/report_coding_agent_benchmark_v1.py",
        "--baseline",
        str(BASELINE_REAL.relative_to(ROOT)),
        "--full-p2",
        str(FULL_REAL.relative_to(ROOT)),
        "--suite",
        suite,
        "--output-dir",
        str(SUMMARY.relative_to(ROOT)),
    ])
    print(
        f"\nBenchmark V1 R2 complete. Summary: "
        f"{SUMMARY / 'benchmark_v1_summary.md'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
