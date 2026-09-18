"""Task-level Coding Agent Evaluation Harness built around the production ExecutionRunner."""
from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import RunResult, RunStatus, Task
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


def materialize_task_repo(task: EvalTask, target: str | Path) -> Path:
    """Create one clean standalone Git repository for a single trial."""
    root = Path(target).resolve()
    if root.exists():
        raise FileExistsError(f"trial repository already exists: {root}")
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
    _run_git(root, "commit", "-qm", "evaluation fixture baseline")
    return root


def apply_reference_solution(task: EvalTask, repo: Path) -> None:
    for relative, content in task.reference_files.items():
        path = (repo / relative).resolve()
        path.relative_to(repo.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _outcome_graders(task: EvalTask):
    return tuple(grader for grader in task.graders if grader.kind != "run_trace")


def validate_reference_solution(task: EvalTask, target: str | Path) -> tuple[GraderResult, ...]:
    if not task.reference_files:
        raise ValueError(f"task {task.task_id!r} has no reference_files")
    repo = materialize_task_repo(task, target)
    apply_reference_solution(task, repo)
    results = grade_many(_outcome_graders(task), GraderContext(repo=repo))
    if not required_graders_passed(results):
        failed = [result.grader_id for result in results if result.required and not result.passed]
        raise ValueError(f"reference solution failed graders for {task.task_id}: {failed}")
    return results


def validate_suite_references(suite: EvaluationSuite, target_root: str | Path) -> dict[str, list[str]]:
    root = Path(target_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, list[str]] = {}
    for task in suite.tasks:
        results = validate_reference_solution(task, root / task.task_id)
        summary[task.task_id] = [result.grader_id for result in results if result.passed]
    return summary


def _fixture_patch(task: EvalTask, repo: Path) -> str:
    after: dict[str, str] = {}
    for path in repo.rglob("*"):
        if not path.is_file() or any(part in _IGNORED_DIRS for part in path.relative_to(repo).parts):
            continue
        try:
            after[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    chunks: list[str] = []
    for relative in sorted(set(task.files) | set(after)):
        before_text, after_text = task.files.get(relative, ""), after.get(relative, "")
        if before_text == after_text:
            continue
        chunks.extend(difflib.unified_diff(
            before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
            fromfile=f"a/{relative}" if relative in task.files else "/dev/null",
            tofile=f"b/{relative}" if relative in after else "/dev/null",
        ))
    return "".join(chunks)


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

    def run(self, task_ids: Iterable[str] = ()) -> list[TrialResult]:
        selected = set(task_ids)
        tasks = [task for task in self.suite.tasks if not selected or task.task_id in selected]
        missing = sorted(selected - {task.task_id for task in self.suite.tasks})
        if missing:
            raise ValueError(f"unknown task id(s): {missing}")
        output = reserve_output_dir(self.output_dir)
        metadata = {
            "schema_version": 1,
            "suite_id": self.suite.suite_id,
            "variant": self.variant,
            "repetitions": self.repetitions,
            "task_count": len(tasks),
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
        repo = materialize_task_repo(task, trial_dir / "repo")
        trace_dir = trial_dir / "traces"
        cached_outcome: tuple[GraderResult, ...] | None = None

        def verifier(workspace: Path) -> bool:
            nonlocal cached_outcome
            cached_outcome = grade_many(_outcome_graders(task), GraderContext(repo=workspace))
            return required_graders_passed(cached_outcome)

        runner = self.runner_factory(task, repo, trace_dir, config.trial_id)
        max_steps = int(self.suite.defaults.get("max_steps", 20))
        budget_tokens = int(self.suite.defaults.get("budget_tokens", 40_000))
        agent_task = Task(
            task.description,
            str(repo),
            task_id=config.trial_id,
            test_cmd=task.test_cmd,
            require_changes=task.require_changes,
            require_tests=task.require_tests,
            max_steps=max_steps,
            budget_tokens=budget_tokens,
        )
        acceptance = AcceptanceContract(
            require_changes=task.require_changes,
            require_tests=task.require_tests,
            verifier=verifier if _outcome_graders(task) else None,
        )
        started = time.perf_counter()
        try:
            run_result = runner.run(RunRequest(task=agent_task, acceptance=acceptance, entrypoint="eval"))
            elapsed = time.perf_counter() - started
            metrics = extract_metrics(run_result, wall_time_seconds=elapsed)
            outcome = cached_outcome or grade_many(_outcome_graders(task), GraderContext(repo=repo))
            process = grade_many(
                (grader for grader in task.graders if grader.kind == "run_trace"),
                GraderContext(repo=repo, run_result=run_result, metrics=metrics),
            )
            graders = outcome + process
            success = (
                run_result.is_success()
                and run_result.acceptance_status in {"not_requested", "passed"}
                and required_graders_passed(graders)
            )
            error = run_result.error
        except Exception as exc:  # harness still emits a TrialResult for one failed trial
            elapsed = time.perf_counter() - started
            run_result = RunResult(
                task_id=config.trial_id,
                status=RunStatus.FAILED,
                summary=f"evaluation runner raised {type(exc).__name__}: {exc}",
                steps_taken=0,
                error=str(exc),
                termination_reason="evaluation_runner_exception",
            )
            metrics = TrialMetrics(wall_time_seconds=elapsed)
            graders = grade_many(_outcome_graders(task), GraderContext(repo=repo))
            success = False
            error = run_result.summary

        patch_path = trial_dir / "patch.diff"
        patch_path.write_text(_fixture_patch(task, repo), encoding="utf-8")
        final_state_path = trial_dir / "final_state.json"
        write_json(final_state_path, _final_state(repo))
        result = TrialResult(
            suite_id=self.suite.suite_id,
            task_id=task.task_id,
            trial_id=config.trial_id,
            variant=config.variant,
            repetition=config.repetition,
            execution_status="executed",
            evidence_kind=self.evidence_kind,
            real_model_executed=self.real_model_executed,
            run_status=run_result.status.value,
            termination_reason=run_result.termination_reason,
            acceptance_status=run_result.acceptance_status,
            success=success,
            metrics=metrics,
            grader_results=graders,
            trace_ref=_relative_ref(run_result.trace_path, self.output_dir),
            patch_ref=patch_path.relative_to(self.output_dir).as_posix(),
            final_state_ref=final_state_path.relative_to(self.output_dir).as_posix(),
            error=error,
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
        "claim_boundary": (
            "The suite/reference validation and harness implementation may be inspected separately; "
            "this artifact contains no real-model success, token, step, or latency claim."
        ),
    }
    write_json(output / "metadata.json", metadata)
    write_json(output / "report.json", report)
    (output / "raw.jsonl").write_text("", encoding="utf-8")
    return report
