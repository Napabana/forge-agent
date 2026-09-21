"""Candidate evaluation wiring that reuses the P2-0 EvaluationHarness."""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.coding_agent.runner import EvaluationHarness, RunnerFactory
from evals.coding_agent.schema import EvalTask, EvaluationSuite, GraderSpec, TrialResult
from experience.schema import EvaluationCase, EvaluationRecord, EvaluationRole, SkillCandidate

RunnerFactoryBuilder = Callable[[Path | None], RunnerFactory]
_EVOLUTION_GRADER_ID = "evolution-skill-trigger"


def evaluation_role(task: EvalTask) -> EvaluationRole:
    tags = set(task.tags)
    mapping = {
        "evolution-target": EvaluationRole.TARGET,
        "evolution-should-trigger": EvaluationRole.SHOULD_TRIGGER,
        "evolution-should-not-trigger": EvaluationRole.SHOULD_NOT_TRIGGER,
        "evolution-non-regression": EvaluationRole.NON_REGRESSION,
    }
    matched = [role for tag, role in mapping.items() if tag in tags]
    if len(matched) > 1:
        raise ValueError(f"task {task.task_id!r} has multiple evolution roles")
    return matched[0] if matched else EvaluationRole.NON_REGRESSION


def _candidate_suite(
    suite: EvaluationSuite,
    candidate: SkillCandidate,
    *,
    process_graders: tuple[GraderSpec, ...] = (),
) -> EvaluationSuite:
    tasks: list[EvalTask] = []
    for task in suite.tasks:
        role = evaluation_role(task)
        params: dict[str, list[str]] | None = None
        if role in {EvaluationRole.TARGET, EvaluationRole.SHOULD_TRIGGER}:
            params = {"expected_any": [candidate.skill_name]}
        elif role is EvaluationRole.SHOULD_NOT_TRIGGER:
            params = {"forbidden": [candidate.skill_name]}
        graders = task.graders
        if params is not None:
            graders = graders + (
                GraderSpec(
                    _EVOLUTION_GRADER_ID,
                    "skill_selection",
                    params,
                    required=False,
                ),
            )
        if (
            role in {EvaluationRole.TARGET, EvaluationRole.SHOULD_TRIGGER}
            and process_graders
        ):
            graders = graders + process_graders
        tasks.append(EvalTask(
            task_id=task.task_id,
            description=task.description,
            files=dict(task.files),
            graders=graders,
            reference_files=dict(task.reference_files),
            require_changes=task.require_changes,
            require_tests=task.require_tests,
            test_cmd=task.test_cmd,
            tags=task.tags,
        ))
    return EvaluationSuite(
        suite_id=suite.suite_id,
        tasks=tuple(tasks),
        description=suite.description,
        defaults=dict(suite.defaults),
        schema_version=suite.schema_version,
    )


def materialize_candidate_skill_root(
    candidate: SkillCandidate,
    target_root: str | Path,
    *,
    base_skill_root: str | Path | None = None,
) -> Path:
    target = Path(target_root).resolve()
    if target.exists():
        raise FileExistsError(f"candidate eval skill root already exists: {target}")
    target.mkdir(parents=True)
    if base_skill_root is not None:
        base = Path(base_skill_root).resolve()
        if base.is_dir():
            for child in sorted(base.iterdir(), key=lambda item: item.name):
                if child.is_symlink() or not child.is_dir():
                    continue
                shutil.copytree(child, target / child.name)
    skill_dir = target / candidate.skill_name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(candidate.skill_markdown(), encoding="utf-8")
    return target


def _required_graders_passed(result: TrialResult) -> bool:
    return all(item.passed for item in result.grader_results if item.required)


def _candidate_loaded(result: TrialResult, candidate_name: str) -> tuple[bool, bool]:
    for grader in result.grader_results:
        if grader.grader_id != _EVOLUTION_GRADER_ID:
            continue
        selected = {str(value) for value in grader.evidence.get("selected_skills", [])}
        return candidate_name in selected, grader.passed
    return False, True


def _evaluation_failed(result: TrialResult) -> tuple[bool, str | None]:
    if result.execution_status != "executed":
        return True, f"execution_status={result.execution_status}"
    if result.termination_reason in {
        "evaluation_runner_exception",
        "evaluation_runner_cleanup_failure",
        "infrastructure_error",
    }:
        return True, result.termination_reason
    return False, None


def build_evaluation_record(
    *,
    candidate: SkillCandidate,
    suite: EvaluationSuite,
    baseline_results: list[TrialResult],
    candidate_results: list[TrialResult],
    baseline_variant: str,
    candidate_variant: str,
) -> EvaluationRecord:
    baseline = {(row.task_id, row.repetition): row for row in baseline_results}
    enabled = {(row.task_id, row.repetition): row for row in candidate_results}
    if set(baseline) != set(enabled):
        raise ValueError("baseline and candidate evaluation trials are incomplete or mismatched")
    tasks = {task.task_id: task for task in suite.tasks}
    cases: list[EvaluationCase] = []
    for key in sorted(baseline):
        task_id, repetition = key
        if task_id not in tasks:
            raise ValueError(f"evaluation result references unknown task: {task_id}")
        left, right = baseline[key], enabled[key]
        loaded, process_passed = _candidate_loaded(right, candidate.skill_name)
        left_failed, left_reason = _evaluation_failed(left)
        right_failed, right_reason = _evaluation_failed(right)
        cases.append(EvaluationCase(
            task_id=task_id,
            repetition=repetition,
            role=evaluation_role(tasks[task_id]),
            baseline_success=left.success,
            candidate_success=right.success,
            baseline_required_graders_passed=_required_graders_passed(left),
            candidate_required_graders_passed=_required_graders_passed(right),
            candidate_loaded=loaded,
            candidate_process_passed=process_passed,
            baseline_steps=left.metrics.steps,
            candidate_steps=right.metrics.steps,
            baseline_tokens=left.metrics.total_tokens,
            candidate_tokens=right.metrics.total_tokens,
            baseline_trial_id=left.trial_id,
            candidate_trial_id=right.trial_id,
            baseline_trace_ref=left.trace_ref,
            candidate_trace_ref=right.trace_ref,
            evaluation_failed=left_failed or right_failed,
            failure_reason=right_reason or left_reason,
        ))
    return EvaluationRecord.build(
        candidate=candidate,
        suite_id=suite.suite_id,
        baseline_variant=baseline_variant,
        candidate_variant=candidate_variant,
        cases=tuple(cases),
    )


def evaluate_candidate(
    *,
    candidate: SkillCandidate,
    suite: EvaluationSuite,
    output_dir: str | Path,
    runner_factory_builder: RunnerFactoryBuilder,
    base_skill_root: str | Path | None = None,
    repetitions: int = 1,
    evidence_kind: str = "deterministic_harness",
    real_model_executed: bool = False,
    candidate_process_graders: tuple[GraderSpec, ...] = (),
    run_metadata: dict[str, Any] | None = None,
) -> EvaluationRecord:
    """Run baseline and candidate variants through the existing P2-0 harness."""
    root = Path(output_dir).resolve()
    if root.exists():
        raise FileExistsError(f"candidate evaluation output already exists: {root}")
    root.mkdir(parents=True)
    overlay = materialize_candidate_skill_root(
        candidate,
        root / "candidate-skills",
        base_skill_root=base_skill_root,
    )
    candidate_suite = _candidate_suite(
        suite,
        candidate,
        process_graders=candidate_process_graders,
    )
    baseline_variant = "evolution_baseline"
    candidate_variant = "evolution_candidate"
    baseline_results = EvaluationHarness(
        suite=suite,
        output_dir=root / "baseline",
        runner_factory=runner_factory_builder(
            Path(base_skill_root).resolve() if base_skill_root else None
        ),
        variant=baseline_variant,
        repetitions=repetitions,
        evidence_kind=evidence_kind,
        real_model_executed=real_model_executed,
        run_metadata={
            **dict(run_metadata or {}),
            "evolution_candidate": None,
        },
    ).run()
    candidate_results = EvaluationHarness(
        suite=candidate_suite,
        output_dir=root / "candidate",
        runner_factory=runner_factory_builder(overlay),
        variant=candidate_variant,
        repetitions=repetitions,
        evidence_kind=evidence_kind,
        real_model_executed=real_model_executed,
        run_metadata={
            **dict(run_metadata or {}),
            "evolution_candidate_id": candidate.candidate_id,
            "evolution_candidate_version": candidate.candidate_version,
            "evolution_candidate_hash": candidate.content_hash,
        },
    ).run()
    record = build_evaluation_record(
        candidate=candidate,
        suite=suite,
        baseline_results=baseline_results,
        candidate_results=candidate_results,
        baseline_variant=baseline_variant,
        candidate_variant=candidate_variant,
    )
    (root / "evaluation.json").write_text(
        __import__("json").dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return record
