"""Verify the frozen P2 Repo Map real-model small-sample artifact.

This is separate from evals.verify_evidence_pack because the original
repo_map_agent_ablation directory remains the provider-free harness artifact.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = (
    "no_repo_map",
    "static_repo_map",
    "query_aware_repo_map",
    "incremental_query_aware_repo_map",
)


def _close(actual: Any, expected: float) -> bool:
    try:
        return math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def verify(root: Path | None = None) -> list[str]:
    repo = (root or ROOT).resolve()
    result_dir = repo / "evals" / "results" / "repo_map_agent_ablation_real_v1"
    errors: list[str] = []
    try:
        metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
        report = json.loads((result_dir / "report.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (result_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        return [f"cannot read P2 real-model evidence: {exc}"]

    if metadata.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if metadata.get("execution_status") != "executed" or metadata.get("real_model_executed") is not True:
        errors.append("real-model artifact must be executed")
    if metadata.get("provider") != "openai" or metadata.get("model") != "deepseek-v4.1-flash":
        errors.append("provider/model changed")
    if metadata.get("repetitions") != 1 or metadata.get("run_count") != 16:
        errors.append("expected 4 cases × 4 variants × 1 repetition = 16 runs")
    if tuple(metadata.get("variants", ())) != VARIANTS:
        errors.append("variant order changed")
    if len(rows) != 16 or report.get("run_count") != 16:
        errors.append("raw/report run count must both be 16")
    if not rows or not all(bool(row.get("verifier_passed")) for row in rows):
        errors.append("all 16 frozen rows must pass the independent verifier")
    if not any(int(row.get("cached_input_tokens", 0)) > 0 for row in rows):
        errors.append("provider-reported cached_input_tokens must be present")

    expected = {
        "no_repo_map": (4, 3, 4, 65055.25, 26048.0, 70.44753804325006),
        "static_repo_map": (4, 0, 4, 96704.25, 39776.0, 96.96836330624996),
        "query_aware_repo_map": (4, 3, 4, 67349.25, 26752.0, 70.39974505924994),
        "incremental_query_aware_repo_map": (4, 4, 4, 59541.0, 21728.0, 64.71446809549997),
    }
    values = report.get("variants", {})
    for variant, (runs, solved, verifier, total, cached, latency) in expected.items():
        row = values.get(variant, {})
        if row.get("runs") != runs:
            errors.append(f"{variant}: runs changed")
        if row.get("observed_solved") != solved:
            errors.append(f"{variant}: observed_solved changed")
        if row.get("observed_verifier_passes") != verifier:
            errors.append(f"{variant}: observed_verifier_passes changed")
        if not _close(row.get("mean_total_tokens"), total):
            errors.append(f"{variant}: mean_total_tokens changed")
        if not _close(row.get("mean_cached_input_tokens"), cached):
            errors.append(f"{variant}: mean_cached_input_tokens changed")
        if not _close(row.get("mean_latency_seconds"), latency):
            errors.append(f"{variant}: mean_latency_seconds changed")
    return errors


def main() -> int:
    errors = verify()
    if errors:
        print("P2 Repo Map real-model evidence verification FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("P2 Repo Map real-model evidence verification passed.")
    print("4 cases × 4 variants × 1 run = 16 real-model runs; all 16 verifier passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
