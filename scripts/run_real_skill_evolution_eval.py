from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agent.core import AgentConfig
from agent.runner import ExecutionRunner
from config.schema import load_config
from evals.coding_agent.runner import validate_suite_references
from evals.coding_agent.schema import (
    EvaluationSuite,
    GraderResult,
    GraderSpec,
    TrialMetrics,
    TrialResult,
)
from experience.evaluation import (
    build_evaluation_record,
    evaluate_candidate,
    evaluation_role,
)
from experience.promotion import PromotionGate
from experience.schema import EvaluationRole, PatternType, SkillCandidate
from llm.router import create_backend_from_config
from tools.base import ToolRegistry
from tools.file_tool import FileEditTool, FileReadTool, FileViewTool, FileWriteTool
from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
from tools.test_tool import PytestTool


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = (
    ROOT / "evals" / "fixtures" / "skill_evolution" / "real_recovery_motif_suite.json"
)


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return ROOT / "evals" / "results" / f"p2-5-real-candidate-eval-{stamp}"


def _trial_result_from_dict(raw: dict[str, Any]) -> TrialResult:
    metrics_raw = dict(raw.get("metrics") or {})
    graders_raw = list(raw.get("grader_results") or ())
    metrics = TrialMetrics(**metrics_raw)
    graders = tuple(
        GraderResult(
            grader_id=str(item.get("grader_id") or ""),
            kind=str(item.get("kind") or ""),
            passed=bool(item.get("passed")),
            required=bool(item.get("required")),
            detail=str(item.get("detail") or ""),
            evidence=dict(item.get("evidence") or {}),
        )
        for item in graders_raw
    )
    return TrialResult(
        suite_id=str(raw.get("suite_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        trial_id=str(raw.get("trial_id") or ""),
        variant=str(raw.get("variant") or ""),
        repetition=int(raw.get("repetition") or 1),
        execution_status=str(raw.get("execution_status") or ""),
        evidence_kind=str(raw.get("evidence_kind") or ""),
        real_model_executed=bool(raw.get("real_model_executed")),
        run_status=str(raw.get("run_status") or ""),
        termination_reason=(
            str(raw["termination_reason"])
            if raw.get("termination_reason") is not None
            else None
        ),
        acceptance_status=str(raw.get("acceptance_status") or ""),
        success=bool(raw.get("success")),
        metrics=metrics,
        grader_results=graders,
        trace_ref=(str(raw["trace_ref"]) if raw.get("trace_ref") is not None else None),
        patch_ref=(str(raw["patch_ref"]) if raw.get("patch_ref") is not None else None),
        final_state_ref=(
            str(raw["final_state_ref"])
            if raw.get("final_state_ref") is not None
            else None
        ),
        error=(str(raw["error"]) if raw.get("error") is not None else None),
    )


def _load_trial_results(path: Path) -> list[TrialResult]:
    if not path.is_file():
        raise SystemExit(f"missing evaluation trial artifact: {path}")
    results: list[TrialResult] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid trial JSON at {path}:{line_no}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise SystemExit(f"trial row at {path}:{line_no} must be an object")
        results.append(_trial_result_from_dict(raw))
    if not results:
        raise SystemExit(f"no trial results found in {path}")
    return results


def _trial_diagnostics(results: list[TrialResult]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for result in results:
        diagnostics.append(
            {
                "task_id": result.task_id,
                "variant": result.variant,
                "run_status": result.run_status,
                "termination_reason": result.termination_reason,
                "acceptance_status": result.acceptance_status,
                "raw_trial_success": result.success,
                "steps": result.metrics.steps,
                "tokens": result.metrics.total_tokens,
                "failure_classified_count": result.metrics.failure_classified_count,
                "recovery_selected_count": result.metrics.recovery_selected_count,
                "skill_discovered_count": result.metrics.skill_discovered_count,
                "skill_selected_count": result.metrics.skill_selected_count,
                "skill_loaded_count": result.metrics.skill_loaded_count,
                "graders": [
                    {
                        "id": grader.grader_id,
                        "kind": grader.kind,
                        "passed": grader.passed,
                        "required": grader.required,
                        "detail": grader.detail,
                        "evidence": grader.evidence,
                    }
                    for grader in result.grader_results
                ],
            }
        )
    return diagnostics


def _summary_payload(
    *,
    candidate: SkillCandidate,
    record,
    decision,
    evaluation_path: Path,
    decision_path: Path,
    mode: str,
    provider_calls: int | str,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "provider_calls": provider_calls,
        "candidate_id": candidate.candidate_id,
        "candidate_skill": candidate.skill_name,
        "pattern_id": candidate.pattern_id,
        "evaluation_record_id": record.record_id,
        "promotion_status": decision.status.value,
        "promotion_reasons": list(decision.reasons),
        "cases": [
            {
                "task_id": case.task_id,
                "role": case.role.value,
                "baseline_success": case.baseline_success,
                "candidate_success": case.candidate_success,
                "baseline_required_graders_passed": (
                    case.baseline_required_graders_passed
                ),
                "candidate_required_graders_passed": (
                    case.candidate_required_graders_passed
                ),
                "candidate_loaded": case.candidate_loaded,
                "candidate_process_passed": case.candidate_process_passed,
                "baseline_steps": case.baseline_steps,
                "candidate_steps": case.candidate_steps,
                "baseline_tokens": case.baseline_tokens,
                "candidate_tokens": case.candidate_tokens,
                "evaluation_failed": case.evaluation_failed,
                "failure_reason": case.failure_reason,
            }
            for case in record.cases
        ],
        "evaluation_path": str(evaluation_path),
        "decision_path": str(decision_path),
        "auto_promoted": False,
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"missing JSON artifact: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid JSON artifact {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return raw


def _resolve_candidate(
    mining_report: Path,
    pattern_id: str,
) -> tuple[SkillCandidate, dict[str, Any], GraderSpec]:
    report = _load_json(mining_report)
    patterns = report.get("patterns")
    candidates = report.get("candidates")
    if not isinstance(patterns, list) or not isinstance(candidates, list):
        raise SystemExit("mining report is missing patterns/candidates")

    pattern = next(
        (
            item
            for item in patterns
            if isinstance(item, dict) and str(item.get("pattern_id") or "") == pattern_id
        ),
        None,
    )
    row = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("pattern_id") or "") == pattern_id
        ),
        None,
    )
    if pattern is None or row is None:
        raise SystemExit(f"pattern {pattern_id!r} was not found in mining report")
    if str(pattern.get("pattern_type") or "") != PatternType.RECOVERY_WORKFLOW.value:
        raise SystemExit("real recovery final-gate runner requires a recovery_workflow pattern")
    if int(pattern.get("evidence_count") or 0) < 2:
        raise SystemExit("candidate does not meet the default source-evidence threshold")
    if row.get("promotion_evidence_ready") is not True:
        raise SystemExit("candidate is not marked promotion_evidence_ready")

    artifact_dir = Path(str(row.get("artifact_dir") or ""))
    if not artifact_dir.is_absolute():
        artifact_dir = (mining_report.parent / artifact_dir).resolve()
    candidate_path = artifact_dir / "candidate.json"
    candidate_raw = _load_json(candidate_path)
    candidate = SkillCandidate.from_dict(candidate_raw)
    if (
        candidate.pattern_id != pattern_id
        or candidate.candidate_id != str(row.get("candidate_id") or "")
        or candidate.content_hash != str(row.get("content_hash") or "")
    ):
        raise SystemExit("candidate artifact does not match mining report identity/hash")

    signature = tuple(str(item) for item in pattern.get("signature") or ())
    failure = next(
        (item.split(":", 1)[1] for item in signature if item.startswith("failure:")),
        None,
    )
    recovery = next(
        (item.split(":", 1)[1] for item in signature if item.startswith("recovery:")),
        None,
    )
    operations = tuple(
        item
        for item in signature
        if not item.startswith(("failure:", "recovery:", "FAIL:", "RECOVER:"))
    )
    if failure is None or recovery is None or len(operations) != 1:
        raise SystemExit(
            "candidate pattern must be one bounded motif: failure -> recovery -> one semantic action"
        )
    process_grader = GraderSpec(
        "evolution-recovery-motif",
        "recovery_motif",
        {
            "failure_category": failure,
            "recovery_strategy": recovery,
            "skill_name": candidate.skill_name,
            "next_operation": operations[0],
        },
        required=False,
    )
    return candidate, pattern, process_grader


def _minimal_registry(repo: Path) -> ToolRegistry:
    return (
        ToolRegistry()
        .register(FileReadTool(workspace=repo))
        .register(FileViewTool(workspace=repo))
        .register(SearchTextTool(workspace=repo))
        .register(FindFilesTool(workspace=repo))
        .register(FindSymbolTool(workspace=repo))
        .register(FileEditTool(workspace=repo))
        .register(FileWriteTool(workspace=repo))
        .register(PytestTool(default_cwd=str(repo)))
    )


def _runner_factory_builder(cfg, backend, suite: EvaluationSuite):
    trigger_threshold = int(suite.defaults.get("trigger_no_progress_steps", 2))
    normal_threshold = int(suite.defaults.get("non_trigger_no_progress_steps", 6))

    def builder(skill_root: Path | None):
        def factory(task, repo, trace_dir, trial_id):
            role = evaluation_role(task)
            threshold = (
                trigger_threshold
                if role in {EvaluationRole.TARGET, EvaluationRole.SHOULD_TRIGGER}
                else normal_threshold
            )
            agent_config = AgentConfig(
                max_steps=int(suite.defaults.get("max_steps", 10)),
                reflection_no_edit_steps=threshold,
                budget_tokens=int(suite.defaults.get("budget_tokens", 12_000)),
                history_max_messages=cfg.context.history_window * 2,
                planning_mode="off",
                recovery_mode="structured",
                recovery_max_attempts=cfg.agent.recovery_max_attempts,
                skills_enabled=skill_root is not None,
                skills_global_dir=str(skill_root) if skill_root is not None else None,
                skills_max_loaded=cfg.agent.skills_max_loaded,
                skills_max_chars=cfg.agent.skills_max_chars,
                skills_reference_max_chars=cfg.agent.skills_reference_max_chars,
                stream=False,
                repo_map_mode="none",
            )
            return ExecutionRunner(
                backend=backend,
                registry=_minimal_registry(repo),
                config=agent_config,
                log_dir=str(trace_dir),
            )

        return factory

    return builder


def _dry_run_payload(
    *,
    candidate: SkillCandidate,
    pattern: dict[str, Any],
    suite: EvaluationSuite,
    process_grader: GraderSpec,
    reference_validation: dict[str, list[str]],
    output_dir: Path,
) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "provider_calls": 0,
        "candidate_id": candidate.candidate_id,
        "candidate_skill": candidate.skill_name,
        "pattern_id": candidate.pattern_id,
        "pattern_signature": list(pattern.get("signature") or ()),
        "evidence_count": int(pattern.get("evidence_count") or 0),
        "process_grader": {
            "id": process_grader.grader_id,
            "kind": process_grader.kind,
            "params": dict(process_grader.params),
            "trial_blocking": process_grader.required,
            "promotion_required": True,
        },
        "suite_id": suite.suite_id,
        "roles": [evaluation_role(task).value for task in suite.tasks],
        "paired_trials": len(suite.tasks) * 2,
        "repetitions": 1,
        "planning_mode": "off",
        "recovery_mode": "structured",
        "repo_map_mode": "none",
        "trigger_no_progress_steps": int(
            suite.defaults.get("trigger_no_progress_steps", 2)
        ),
        "non_trigger_no_progress_steps": int(
            suite.defaults.get("non_trigger_no_progress_steps", 6)
        ),
        "max_steps": int(suite.defaults.get("max_steps", 10)),
        "budget_tokens": int(suite.defaults.get("budget_tokens", 12_000)),
        "reference_validation": reference_validation,
        "output_dir": str(output_dir),
        "claim_boundary": (
            "Dry-run only. No model backend was created and no Agent capability "
            "result exists yet."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mining-report", required=True)
    parser.add_argument("--pattern-id", required=True)
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly run the configured real model. Without this flag the command is 0-API.",
    )
    mode.add_argument(
        "--replay-existing",
        action="store_true",
        help=(
            "Rebuild EvaluationRecord/PromotionDecision from existing baseline/candidate "
            "trial artifacts without creating a model backend."
        ),
    )
    args = parser.parse_args(argv)

    mining_report = Path(args.mining_report).resolve()
    suite = EvaluationSuite.load(args.suite)
    candidate, pattern, process_grader = _resolve_candidate(
        mining_report,
        str(args.pattern_id),
    )
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else _default_output_dir().resolve()
    )

    if args.replay_existing:
        baseline_results = _load_trial_results(output_dir / "baseline" / "raw.jsonl")
        candidate_results = _load_trial_results(output_dir / "candidate" / "raw.jsonl")
        record = build_evaluation_record(
            candidate=candidate,
            suite=suite,
            baseline_results=baseline_results,
            candidate_results=candidate_results,
            baseline_variant="evolution_baseline",
            candidate_variant="evolution_candidate",
        )
        decision = PromotionGate().evaluate(candidate, record)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        replay_dir = output_dir / f"reanalysis-{stamp}"
        replay_dir.mkdir(parents=True, exist_ok=False)
        evaluation_path = replay_dir / "evaluation.json"
        decision_path = replay_dir / "promotion_decision.json"
        summary_path = replay_dir / "final_gate_summary.json"
        evaluation_path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        decision_path.write_text(
            json.dumps(decision.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = _summary_payload(
            candidate=candidate,
            record=record,
            decision=decision,
            evaluation_path=evaluation_path,
            decision_path=decision_path,
            mode="replay_existing",
            provider_calls=0,
        )
        summary["source_baseline_trials"] = str(output_dir / "baseline" / "raw.jsonl")
        summary["source_candidate_trials"] = str(output_dir / "candidate" / "raw.jsonl")
        summary["baseline_trial_diagnostics"] = _trial_diagnostics(baseline_results)
        summary["candidate_trial_diagnostics"] = _trial_diagnostics(candidate_results)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="forge-p2-5-real-gate-ref-") as temp_dir:
        reference_validation = validate_suite_references(suite, temp_dir)

    if not args.execute:
        print(
            json.dumps(
                _dry_run_payload(
                    candidate=candidate,
                    pattern=pattern,
                    suite=suite,
                    process_grader=process_grader,
                    reference_validation=reference_validation,
                    output_dir=output_dir,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    cfg = load_config(args.config)
    if not cfg.llm.api_key:
        raise SystemExit("configured provider credentials are missing; refusing real-model execution")
    backend = create_backend_from_config(
        {
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "api_key": cfg.llm.api_key or None,
            "base_url": cfg.llm.base_url or None,
            "max_tokens": cfg.llm.max_tokens,
        }
    )

    record = evaluate_candidate(
        candidate=candidate,
        suite=suite,
        output_dir=output_dir,
        runner_factory_builder=_runner_factory_builder(cfg, backend, suite),
        repetitions=1,
        evidence_kind="real_model",
        real_model_executed=True,
        candidate_process_graders=(process_grader,),
        run_metadata={
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "planning_mode": "off",
            "recovery_mode": "structured",
            "repo_map_mode": "none",
            "trigger_no_progress_steps": int(
                suite.defaults.get("trigger_no_progress_steps", 2)
            ),
            "non_trigger_no_progress_steps": int(
                suite.defaults.get("non_trigger_no_progress_steps", 6)
            ),
            "candidate_pattern_id": candidate.pattern_id,
        },
    )
    decision = PromotionGate().evaluate(candidate, record)
    decision_path = output_dir / "promotion_decision.json"
    decision_path.write_text(
        json.dumps(decision.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = _summary_payload(
        candidate=candidate,
        record=record,
        decision=decision,
        evaluation_path=output_dir / "evaluation.json",
        decision_path=decision_path,
        mode="execute",
        provider_calls="real_model",
    )
    summary_path = output_dir / "final_gate_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
