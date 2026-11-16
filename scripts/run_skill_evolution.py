#!/usr/bin/env python3
"""Offline real-trace mining entrypoint for P2-5 Skill Evolution.

This script never constructs an LLM backend and never runs EvaluationHarness.
It only loads immutable Trace v2 artifacts, mines deterministic experience
patterns, generates deterministic Skill candidates, and optionally persists
those candidates in the existing project-bounded CandidateStore.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "evals" / "results"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _load_batch_summary(path: Path) -> list[Path]:
    if not path.is_file():
        raise ValueError(f"batch summary does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid batch summary: {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("batch summary must be a JSON object")
    traces = raw.get("eligible_traces_for_p2_5")
    if not isinstance(traces, list):
        raise ValueError("batch summary must contain eligible_traces_for_p2_5 list")
    result: list[Path] = []
    for value in traces:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("eligible_traces_for_p2_5 entries must be non-empty strings")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        result.append(candidate.resolve())
    return result


def _trace_paths(explicit: Sequence[str], batch_summary: str | None) -> list[Path]:
    paths = [Path(value).resolve() for value in explicit]
    if batch_summary:
        paths.extend(_load_batch_summary(Path(batch_summary).resolve()))
    unique: dict[str, Path] = {}
    for path in paths:
        unique[str(path)] = path
    ordered = sorted(unique.values(), key=lambda item: str(item))
    if not ordered:
        raise ValueError("provide at least one --trace or --batch-summary")
    return ordered


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _persist_or_reuse(store: Any, candidate: Any) -> tuple[Any, Path, bool]:
    try:
        path = store.save_candidate(candidate)
        return candidate, path, False
    except FileExistsError:
        existing = store.load_candidate(
            candidate.candidate_id,
            candidate.candidate_version,
        )
        if (
            existing.content_hash != candidate.content_hash
            or existing.pattern_id != candidate.pattern_id
            or existing.skill_name != candidate.skill_name
        ):
            raise ValueError(
                "existing candidate identity collides with different content/provenance"
            )
        path = (
            store.root
            / "candidates"
            / existing.candidate_id
            / f"v{existing.candidate_version:03d}"
        )
        return existing, path, True


def _trajectory_report(loaded: Any) -> dict[str, Any]:
    normalized = loaded.normalized
    return {
        "run_id": normalized.ref.run_id,
        "task_id": normalized.ref.task_id,
        "trace_ref": normalized.ref.trace_ref,
        "trace_sha256": normalized.ref.trace_sha256,
        "run_status": normalized.ref.run_status,
        "acceptance_status": normalized.ref.acceptance_status,
        "termination_reason": normalized.ref.termination_reason,
        "eligible": normalized.eligible,
        "eligibility_reason": normalized.eligibility_reason,
        "workflow": list(normalized.workflow),
        "failure_categories": list(normalized.failure_categories),
        "recovery_strategies": list(normalized.recovery_strategies),
        "loaded_skills": list(normalized.loaded_skills),
        "tools": list(normalized.tools),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        help="existing repository root used by CandidateStore",
    )
    parser.add_argument(
        "--trace",
        action="append",
        default=[],
        help="Trace v2 JSONL input; repeat for multiple traces",
    )
    parser.add_argument(
        "--batch-summary",
        default=None,
        help="batch_summary.json containing eligible_traces_for_p2_5",
    )
    parser.add_argument(
        "--mine-only",
        action="store_true",
        help="explicitly select the offline 0-API mining mode",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="generate report artifacts without writing CandidateStore",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="report directory; defaults to evals/results/skill-evolution-mine-<UTC>",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.mine_only:
        raise SystemExit(
            "only offline --mine-only is supported by this entrypoint; "
            "real-model evaluation is intentionally a separate explicit step"
        )

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"--repo must be an existing directory: {repo}")

    try:
        traces = _trace_paths(args.trace, args.batch_summary)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / f"skill-evolution-mine-{_utc_timestamp()}"
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Deliberately import only the offline P2-5 modules after CLI validation.
    from experience.candidate import (
        CANDIDATE_RENDERER_VERSION,
        DeterministicCandidateGenerator,
    )
    from experience.promotion import PromotionGateConfig
    from experience.store import CandidateStore
    from experience.trajectory import (
        MINING_STRATEGY_VERSION,
        ExperienceMiner,
        load_trajectory,
    )

    loaded = []
    for trace in traces:
        try:
            loaded.append(load_trajectory(trace))
        except Exception as exc:
            raise SystemExit(
                f"failed to load trajectory {trace}: {type(exc).__name__}: {exc}"
            ) from exc

    normalized = tuple(item.normalized for item in loaded)
    eligible = tuple(item for item in normalized if item.eligible)
    patterns = ExperienceMiner().mine(normalized)
    min_evidence = PromotionGateConfig().min_evidence_count

    store = None if args.no_store else CandidateStore(repo)
    generator = DeterministicCandidateGenerator()
    candidate_rows: list[dict[str, Any]] = []

    for pattern in patterns:
        generated = generator.generate(pattern)
        persisted = generated
        store_path: str | None = None
        reused = False
        if store is not None:
            persisted, path, reused = _persist_or_reuse(store, generated)
            store_path = str(path)

        artifact_dir = output_dir / "candidates" / persisted.skill_name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json(artifact_dir / "candidate.json", persisted.to_dict())
        (artifact_dir / "SKILL.md").write_text(
            persisted.skill_markdown(),
            encoding="utf-8",
        )
        candidate_rows.append({
            "candidate_id": persisted.candidate_id,
            "candidate_version": persisted.candidate_version,
            "skill_name": persisted.skill_name,
            "description": persisted.description,
            "content_hash": persisted.content_hash,
            "pattern_id": persisted.pattern_id,
            "pattern_type": persisted.pattern_type.value,
            "evidence_count": pattern.evidence_count,
            "promotion_min_evidence_count": min_evidence,
            "promotion_evidence_ready": pattern.evidence_count >= min_evidence,
            "candidate_store_path": store_path,
            "candidate_store_reused": reused,
            "artifact_dir": str(artifact_dir),
        })

    report = {
        "schema_version": 1,
        "mode": "mine_only",
        "mining_strategy": MINING_STRATEGY_VERSION,
        "candidate_renderer": CANDIDATE_RENDERER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "provider_calls": 0,
        "evaluation_executed": False,
        "input_traces": [str(path) for path in traces],
        "trajectory_count": len(normalized),
        "eligible_trajectory_count": len(eligible),
        "trajectories": [_trajectory_report(item) for item in loaded],
        "pattern_count": len(patterns),
        "patterns": [pattern.to_dict() for pattern in patterns],
        "candidate_count": len(candidate_rows),
        "candidates": candidate_rows,
        "promotion_min_evidence_count": min_evidence,
    }
    report_path = output_dir / "mining_report.json"
    _write_json(report_path, report)

    print("\nP2-5 real-trace mining — OFFLINE / API calls = 0")
    print(f"Repo        : {repo}")
    print(f"Traces      : {len(normalized)} total / {len(eligible)} eligible")
    print(f"Strategy    : {MINING_STRATEGY_VERSION}")
    print(f"Renderer    : {CANDIDATE_RENDERER_VERSION}")
    print(f"Patterns    : {len(patterns)}")
    print(f"Candidates  : {len(candidate_rows)}")
    print(f"Store       : {'disabled' if store is None else store.root}")
    print(f"Report      : {report_path}")

    for index, pattern in enumerate(patterns, start=1):
        row = candidate_rows[index - 1]
        print(f"\nPattern {index}: {pattern.pattern_id}")
        print(f"  type      : {pattern.pattern_type.value}")
        print(f"  evidence  : {pattern.evidence_count}")
        print(f"  signature : {' -> '.join(pattern.signature)}")
        print(f"  candidate : {row['skill_name']} ({row['candidate_id']})")
        print(f"  trigger   : {row['description']}")
        print(
            "  gate-ready: "
            + (
                "yes"
                if row["promotion_evidence_ready"]
                else f"no (needs >= {min_evidence} matching source trajectories)"
            )
        )

    if not patterns:
        print("\nNo deterministic experience patterns were mined from the eligible traces.")
    elif not any(row["promotion_evidence_ready"] for row in candidate_rows):
        print(
            "\nNo candidate currently meets the default PromotionGate source-evidence "
            f"threshold ({min_evidence}). Do not spend real-model evaluation tokens yet."
        )
    else:
        print(
            "\nAt least one candidate meets the source-evidence threshold. "
            "Review mining_report.json and SKILL.md before any real-model evaluation."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
