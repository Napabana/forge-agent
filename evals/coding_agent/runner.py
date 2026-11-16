"""Task-level Coding Agent Evaluation Harness built around the production ExecutionRunner."""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import RunResult, RunStatus, Task
from agent.trace_v2 import redact_trace_string
from evals.coding_agent.graders import (
    GraderContext,
    extract_metrics,
    grade_many,
    required_graders_passed,
)
from evals.coding_agent.report import write_json, write_report_artifacts
from evals.coding_agent.schema import (
    EvalTask,
    EvaluationSuite,
    GraderResult,
    TrialConfig,
    TrialMetrics,
    TrialResult,
)
from evals.trial_outcome_probe import (
    FailureDomain, RolloutStatus, ScoringPolicy, TrialEvidence, VerifierStatus, decide_trial,
)
from examples.harbor.trace_evidence import analyze_trace

RunnerFactory = Callable[[EvalTask, Path, Path, str], ExecutionRunner]
_IGNORED_DIRS = {".git", ".pytest_cache", "__pycache__", "logs", "traces"}


def reserve_output_dir(path: str | Path) -> Path:
    output = Path(path).resolve()
    if output.exists():
        raise FileExistsError(f"evaluation output already exists; refusing to overwrite: {output}")
    output.mkdir(parents=True)
    return output


def _run_git(repo: Path, *args: str) -> None:
    completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")


def _resolve_source_commit(source_repo: Path, source_commit: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", f"{source_commit}^{{commit}}"],
        cwd=source_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"source commit {source_commit} is not available in {source_repo}: "
            f"{completed.stderr.strip()}"
        )
    resolved = completed.stdout.strip().lower()
    if resolved != source_commit:
        raise ValueError(
            f"source commit mismatch: requested={source_commit}, resolved={resolved}"
        )
    return resolved


def materialize_task_repo(
    task: EvalTask,
    target: str | Path,
    *,
    source_repo_path: str | Path | None = None,
    source_commit: str | None = None,
) -> Path:
    """Create one clean standalone Git repository for a single trial.

    Synthetic suites start from task.files as before. Real-repository suites
    clone an exact local source commit, strip its Git history so the pre-setup
    implementation cannot leak through git show, apply task.files as a
    task-specific setup overlay, then create a fresh baseline commit.
    """
    root = Path(target).resolve()
    if root.exists():
        raise FileExistsError(f"trial repository already exists: {root}")

    if source_repo_path is not None:
        if not source_commit:
            raise ValueError("source_commit is required with source_repo_path")
        source = Path(source_repo_path).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"source repository not found: {source}")
        _resolve_source_commit(source, source_commit)
        completed = subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(source), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git clone failed: {completed.stderr.strip()}")
        _run_git(root, "checkout", "--detach", "-q", source_commit)
        shutil.rmtree(root / ".git")
    else:
        if not task.files:
            raise ValueError(f"synthetic task {task.task_id!r} requires fixture files")
        root.mkdir(parents=True)

    for relative, content in task.files.items():
        path = (root / relative).resolve()
        path.relative_to(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "forge-agent-eval@example.invalid")
    _run_git(root, "config", "user.name", "Forge Agent Eval")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "--allow-empty", "-qm", "evaluation task baseline")
    return root

def apply_reference_solution(task: EvalTask, repo: Path) -> None:
    for relative, content in task.reference_files.items():
        path = (repo / relative).resolve()
        path.relative_to(repo.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _outcome_graders(task: EvalTask):
    return tuple(
        grader
        for grader in task.graders
        if grader.kind not in {"run_trace", "skill_selection", "recovery_motif"}
    )


def validate_reference_solution(
    task: EvalTask,
    target: str | Path,
    *,
    source_repo_path: str | Path | None = None,
    source_commit: str | None = None,
) -> tuple[GraderResult, ...]:
    if not task.reference_files:
        raise ValueError(f"task {task.task_id!r} has no reference_files")
    repo = materialize_task_repo(
        task,
        target,
        source_repo_path=source_repo_path,
        source_commit=source_commit,
    )
    apply_reference_solution(task, repo)
    results = grade_many(_outcome_graders(task), GraderContext(repo=repo))
    if not required_graders_passed(results):
        failed = [result.grader_id for result in results if result.required and not result.passed]
        raise ValueError(f"reference solution failed graders for {task.task_id}: {failed}")
    return results


def validate_suite_references(
    suite: EvaluationSuite,
    target_root: str | Path,
    *,
    source_repo_path: str | Path | None = None,
) -> dict[str, list[str]]:
    root = Path(target_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_commit = suite.source.commit if suite.source is not None else None
    if suite.source is not None and source_repo_path is None:
        raise ValueError(
            "this evaluation suite requires --source-repo pointing to a local clone "
            f"that contains {suite.source.repository}@{suite.source.commit}"
        )
    summary: dict[str, list[str]] = {}
    for task in suite.tasks:
        results = validate_reference_solution(
            task,
            root / task.task_id,
            source_repo_path=source_repo_path,
            source_commit=source_commit,
        )
        summary[task.task_id] = [result.grader_id for result in results if result.passed]
    return summary


def _fixture_patch(task: EvalTask, repo: Path) -> str:
    """Return only Agent changes relative to the clean task baseline commit."""
    completed = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff failed: {completed.stderr.strip()}")
    return completed.stdout

def _final_state(repo: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    for path in repo.rglob("*"):
        if not path.is_file() or any(part in _IGNORED_DIRS for part in path.relative_to(repo).parts):
            continue
        relative = path.relative_to(repo).as_posix()
        state[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _relative_ref(path: str | None, output_dir: Path) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(output_dir).as_posix()
    except ValueError:
        return str(path)


class EvaluationHarness:
    """Evaluation outer loop; the Agent execution loop remains ExecutionRunner → Agent."""

    def __init__(
        self,
        *,
        suite: EvaluationSuite,
        output_dir: str | Path,
        runner_factory: RunnerFactory,
        variant: str = "baseline_react",
        repetitions: int = 1,
        evidence_kind: str = "deterministic_harness",
        real_model_executed: bool = False,
        run_metadata: dict[str, Any] | None = None,
        source_repo_path: str | Path | None = None,
        scoring_policy: ScoringPolicy | None = None,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions must be >= 1")
        TrialConfig(suite.suite_id, suite.tasks[0].task_id, variant, 1)
        if real_model_executed and evidence_kind != "real_model":
            raise ValueError("real_model_executed requires evidence_kind='real_model'")
        self.suite = suite
        self.output_dir = Path(output_dir).resolve()
        self.runner_factory = runner_factory
        self.variant = variant
        self.repetitions = repetitions
        self.evidence_kind = evidence_kind
        self.real_model_executed = real_model_executed
        self.run_metadata = dict(run_metadata or {})
        self.scoring_policy = scoring_policy or ScoringPolicy("forge-eval-snapshot-v2")
        self.source_repo_path = (
            Path(source_repo_path).resolve() if source_repo_path is not None else None
        )
        if self.suite.source is not None and self.source_repo_path is None:
            raise ValueError("real-repository suite requires source_repo_path")

    def run(self, task_ids: Iterable[str] = ()) -> list[TrialResult]:
        selected = set(task_ids)
        tasks = [task for task in self.suite.tasks if not selected or task.task_id in selected]
        #防止输入不存在task
        missing = sorted(selected - {task.task_id for task in self.suite.tasks})
        if missing:
            raise ValueError(f"unknown task id(s): {missing}")
        output = reserve_output_dir(self.output_dir)
        metadata = {
            "schema_version": 2,
            "scoring_policy": {
                "id": self.scoring_policy.identifier,
                "fingerprint": self.scoring_policy.fingerprint,
            },
            "suite_id": self.suite.suite_id,
            "variant": self.variant,
            "repetitions": self.repetitions,
            "task_count": len(tasks),
            "tasks": [task.task_id for task in tasks],
            "planned_trial_count": len(tasks) * self.repetitions,
            "execution_status": "executed",
            "evidence_kind": self.evidence_kind,
            "real_model_executed": self.real_model_executed,
            "run_metadata": self.run_metadata,
            "claim_boundary": (
                "Scripted/fake backend executions validate harness behavior only and are not Agent capability evidence."
                if not self.real_model_executed
                else "Observed real-model trials are a small sample until repeated and frozen."
            ),
        }
        write_json(output / "metadata.json", metadata)
        results: list[TrialResult] = []
        for repetition in range(1, self.repetitions + 1):
            for task in tasks:
                config = TrialConfig(self.suite.suite_id, task.task_id, self.variant, repetition)
                result = self._run_trial(task, config)
                results.append(result)
        write_report_artifacts(output, results, suite_id=self.suite.suite_id)
        return results

    def _run_trial(self, task: EvalTask, config: TrialConfig) -> TrialResult:
        trial_dir = self.output_dir / "trials" / config.trial_id
        trial_dir.mkdir(parents=True, exist_ok=False)
        repo = trial_dir / "repo"
        trace_dir = trial_dir / "traces"
        cached_outcome: tuple[GraderResult, ...] | None = None
        phase_errors: list[dict[str, str]] = []
        phase = "materialize"
        runner = None
        run_result = None
        graders: tuple[GraderResult, ...] = ()
        verifier_status = VerifierStatus.NOT_RUN
        patch_ref = final_state_ref = None
        snapshot_available = False
        failure_domain = FailureDomain.NONE
        trace_evidence: dict[str, Any] = {}

        def verifier(workspace: Path) -> bool:
            nonlocal cached_outcome# 缓存结果
            cached_outcome = grade_many(_outcome_graders(task), GraderContext(repo=workspace))
            if any(result.evidence.get("verifier_error") for result in cached_outcome):
                raise RuntimeError("independent verifier could not execute")
            return required_graders_passed(cached_outcome)

        started = time.perf_counter()
        try:
            materialize_task_repo(
                task, repo, source_repo_path=self.source_repo_path,
                source_commit=self.suite.source.commit if self.suite.source else None,
            )
            phase = "runner_factory"
            runner = self.runner_factory(task, repo, trace_dir, config.trial_id)
            agent_task = Task(
                task.description, str(repo), task_id=config.trial_id,
                test_cmd=task.test_cmd, require_changes=task.require_changes,
                require_tests=task.require_tests,
                max_steps=int(self.suite.defaults.get("max_steps", 20)),
                budget_tokens=int(self.suite.defaults.get("budget_tokens", 40_000)),
            )
            acceptance = AcceptanceContract(
                require_changes=task.require_changes, require_tests=task.require_tests,
                verifier=verifier if _outcome_graders(task) else None,
            )
            phase = "runner"
            run_result = runner.run(RunRequest(
                task=agent_task, acceptance=acceptance, entrypoint="eval",
                execution_constraints=task.execution_constraints,
            ))
            failure_domain = {
                "provider_error": FailureDomain.PROVIDER,
                "infrastructure_error": FailureDomain.HARNESS,
                "canceled": FailureDomain.CANCELLATION,
            }.get(run_result.termination_reason, FailureDomain.NONE)
            if run_result.status == RunStatus.FAILED and failure_domain == FailureDomain.NONE:
                failure_domain = FailureDomain.UNKNOWN
            phase = "grading"
            outcome = cached_outcome if cached_outcome is not None else grade_many(
                _outcome_graders(task), GraderContext(repo=repo)
            )
            if not outcome:
                verifier_status = VerifierStatus.NOT_RUN
            elif any(result.evidence.get("verifier_error") for result in outcome):
                verifier_status = VerifierStatus.ERROR
                failure_domain = FailureDomain.HARNESS
            else:
                verifier_status = VerifierStatus.PASSED if required_graders_passed(outcome) else VerifierStatus.FAILED
            metrics = extract_metrics(run_result, wall_time_seconds=time.perf_counter() - started)
            process = grade_many(
                (
                    grader
                    for grader in task.graders
                    if grader.kind in {"run_trace", "skill_selection", "recovery_motif"}
                ),
                GraderContext(repo=repo, run_result=run_result, metrics=metrics),
            )
            graders = outcome + process
        except Exception as exc:
            phase_errors.append({"phase": phase, "type": type(exc).__name__, "message": str(exc)})
            failure_domain = FailureDomain.TASK_ENVIRONMENT if phase == "materialize" else FailureDomain.HARNESS
            if phase == "grading":
                verifier_status = VerifierStatus.ERROR
            if run_result is None:
                run_result = RunResult(
                    config.trial_id, RunStatus.FAILED, "evaluation phase failed", 0,
                    error=str(exc), termination_reason=f"evaluation_{phase}_exception",
                )
        finally:
            # 先固定受测快照，再清理；清理故障不能改写已存在的模型/Verifier结果。
            if repo.is_dir():
                try:
                    patch_path = trial_dir / "patch.diff"
                    patch_path.write_text(_fixture_patch(task, repo), encoding="utf-8")
                    final_state_path = trial_dir / "final_state.json"
                    write_json(final_state_path, _final_state(repo))
                    patch_ref = patch_path.relative_to(self.output_dir).as_posix()
                    final_state_ref = final_state_path.relative_to(self.output_dir).as_posix()
                    snapshot_available = True
                except Exception as exc:
                    phase_errors.append({"phase": "snapshot", "type": type(exc).__name__, "message": str(exc)})
                    failure_domain = FailureDomain.HARNESS
            close = getattr(runner, "close", None) if runner is not None else None
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    phase_errors.append({"phase": "cleanup", "type": type(exc).__name__, "message": str(exc)})

        assert run_result is not None
        try:
            metrics = extract_metrics(run_result, wall_time_seconds=time.perf_counter() - started)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            phase_errors.append({"phase": "metrics", "type": type(exc).__name__, "message": str(exc)})
            metrics = TrialMetrics(steps=run_result.steps_taken, wall_time_seconds=time.perf_counter() - started)
            failure_domain = FailureDomain.HARNESS
        if run_result.trace_path:
            try:
                trace_evidence = analyze_trace(run_result.trace_path)
            except (OSError, ValueError) as exc:
                phase_errors.append({"phase": "trace", "type": type(exc).__name__, "message": str(exc)})
                failure_domain = FailureDomain.HARNESS
        phase_errors = [{**error, "message": redact_trace_string(error["message"])} for error in phase_errors]
        rollout = {
            "success": RolloutStatus.FINISH, "incomplete": RolloutStatus.INCOMPLETE,
            "max_steps": RolloutStatus.INCOMPLETE, "gave_up": RolloutStatus.GAVE_UP,
            "canceled": RolloutStatus.CANCELED, "failed": RolloutStatus.FAILED,
        }.get(run_result.status.value, RolloutStatus.UNKNOWN)
        call_started = bool(trace_evidence.get("started_calls"))
        decision = decide_trial(TrialEvidence(
            config.trial_id, call_started, rollout, failure_domain, verifier_status,
            snapshot_available, snapshot_available,
        ), self.scoring_policy)
        success = (
            run_result.is_success()
            and run_result.acceptance_status in {"not_requested", "passed"}
            and required_graders_passed(graders)
            and verifier_status is VerifierStatus.PASSED
        )
        result = TrialResult(
            suite_id=self.suite.suite_id,
            task_id=task.task_id,
            trial_id=config.trial_id,
            variant=config.variant,
            repetition=config.repetition,
            execution_status="executed",
            evidence_kind=self.evidence_kind,
            real_model_executed=self.real_model_executed and call_started,
            run_status=run_result.status.value,
            termination_reason=run_result.termination_reason,
            acceptance_status=run_result.acceptance_status,
            success=success,
            metrics=metrics,
            grader_results=graders,
            trace_ref=_relative_ref(run_result.trace_path, self.output_dir),
            patch_ref=patch_ref,
            final_state_ref=final_state_ref,
            error=redact_trace_string(run_result.error) if run_result.error else None,
            execution_constraints=task.execution_constraints,
            schema_version=2,
            model_call_started=call_started,
            model_calls_finished=trace_evidence.get("finished_calls"),
            usage_complete=(
                not trace_evidence["usage_is_lower_bound"]
                and getattr(run_result, "usage_complete", None) is not False
                if call_started else None
            ),
            verifier_status=verifier_status.value,
            failure_domain=failure_domain.value,
            scoreable=decision.scoreable,
            scoreability_reason=decision.scoreability_reason,
            scoreable_success=decision.scoreable_success,
            scoring_policy_id=self.scoring_policy.identifier,
            scoring_policy_fingerprint=self.scoring_policy.fingerprint,
            phase_errors=tuple(phase_errors),
        )
        write_json(trial_dir / "trial.json", result.to_dict())
        return result


def write_not_executed(
    suite: EvaluationSuite,
    output_dir: str | Path,
    *,
    variant: str = "baseline_react",
    repetitions: int = 1,
    task_ids: Iterable[str] = (),
    reason: str = "provider_credentials_not_available",
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    TrialConfig(suite.suite_id, suite.tasks[0].task_id, variant, 1)
    selected = set(task_ids)
    tasks = [task for task in suite.tasks if not selected or task.task_id in selected]
    missing = sorted(selected - {task.task_id for task in suite.tasks})
    if missing:
        raise ValueError(f"unknown task id(s): {missing}")
    output = reserve_output_dir(output_dir)
    planned = len(tasks) * repetitions
    metadata = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "variant": variant,
        "repetitions": repetitions,
        "task_count": len(tasks),
        "tasks": [task.task_id for task in tasks],
        "planned_trial_count": planned,
        "execution_status": "not_executed",
        "real_model_executed": False,
        "reason": reason,
        "run_metadata": dict(run_metadata or {}),
        "claim_boundary": "No Agent capability result exists in this artifact.",
    }
    report = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "execution_status": "not_executed",
        "real_model_executed": False,
        "trial_count": 0,
        "task_count": len(tasks),
        "tasks": [task.task_id for task in tasks],
        "planned_trial_count": planned,
        "variants": [variant],
        "reason": reason,
        "run_metadata": dict(run_metadata or {}),
        "claim_boundary": (
            "The suite/reference validation and harness implementation may be inspected separately; "
            "this artifact contains no real-model success, token, step, or latency claim."
        ),
    }
    write_json(output / "metadata.json", metadata)
    write_json(output / "report.json", report)
    (output / "raw.jsonl").write_text("", encoding="utf-8")
    return report
