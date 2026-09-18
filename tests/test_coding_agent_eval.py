from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core import AgentConfig
from agent.runner import ExecutionRunner
from agent.task import Action, ActionType, RunResult, RunStatus, ToolCall
from evals.coding_agent.graders import GraderContext, extract_metrics, grade_many, required_graders_passed
from evals.coding_agent.report import build_report
from evals.coding_agent.runner import EvaluationHarness, materialize_task_repo, reserve_output_dir, validate_suite_references, write_not_executed
from evals.coding_agent.schema import EvalTask, EvaluationSuite, GraderResult, GraderSpec, TrialConfig, TrialMetrics, TrialResult
from llm.base import MockBackend
from llm.usage import SessionUsage
from tools.base import ToolRegistry
from tools.file_tool import FileWriteTool

FIXTURE = Path(__file__).parents[1] / "evals" / "fixtures" / "coding_agent" / "suite.json"


def _task(**overrides):
    data = {
        "id": "tiny-task",
        "description": "Change value.txt to done.",
        "files": {"value.txt": "todo\n"},
        "reference_files": {"value.txt": "done\n"},
        "require_changes": True,
        "graders": [{"id": "file", "kind": "file", "params": {"path": "value.txt", "equals": "done\n"}}],
    }
    data.update(overrides)
    return EvalTask.from_dict(data)


def test_eval_task_schema_validation_and_duplicate_task_ids():
    with pytest.raises(ValueError, match="safe relative path"):
        _task(files={"../escape.txt": "x"})
    task = _task()
    with pytest.raises(ValueError, match="duplicate task id"):
        EvaluationSuite("suite", (task, task))


def test_trial_id_is_stable_across_reconstruction():
    left = TrialConfig("suite", "tiny-task", "baseline_react", 2)
    right = TrialConfig("suite", "tiny-task", "baseline_react", 2)
    assert left.trial_id == right.trial_id == "tiny-task--baseline_react--r002"


def test_materialized_repositories_are_clean_and_isolated(tmp_path: Path):
    task = _task()
    first = materialize_task_repo(task, tmp_path / "first")
    (first / "value.txt").write_text("contaminated\n", encoding="utf-8")
    second = materialize_task_repo(task, tmp_path / "second")
    assert (second / "value.txt").read_text(encoding="utf-8") == "todo\n"
    assert not (second / ".git" / "index.lock").exists()


def test_graders_success_failure_and_aggregation(tmp_path: Path):
    repo = materialize_task_repo(_task(), tmp_path / "repo")
    specs = (
        GraderSpec("exists", "file", {"path": "value.txt", "contains": "todo"}),
        GraderSpec("command", "command", {"command": ["{python}", "-c", "assert open('value.txt').read() == 'todo\\n'"]}),
    )
    results = grade_many(specs, GraderContext(repo))
    assert required_graders_passed(results)
    failed = grade_many((GraderSpec("wrong", "file", {"path": "value.txt", "contains": "missing"}),), GraderContext(repo))
    assert not required_graders_passed(failed)


def test_metrics_extract_from_run_result_and_trace(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    events = [
        {"event_type": "tool_execution_started", "payload": {"tool_name": "file_read"}},
        {"event_type": "tool_execution_started", "payload": {"tool_name": "pytest"}},
        {"event_type": "tool_execution_started", "payload": {"tool_name": "shell"}},
        {"event_type": "completion_rejected", "payload": {}},
        {"event_type": "reflection", "payload": {}},
        {"event_type": "plan_created", "payload": {}},
        {"event_type": "plan_step_completed", "payload": {"step_status": "completed"}},
        {"event_type": "plan_revised", "payload": {}},
        {"event_type": "planning_skipped", "payload": {}},
        {"event_type": "failure_classified", "payload": {"category": "tool_failure"}},
        {"event_type": "recovery_selected", "payload": {"strategy": "replan"}},
        {"event_type": "recovery_exhausted", "payload": {}},
    ]
    trace.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    usage = SessionUsage(input_tokens=20, output_tokens=5, llm_calls=1)
    result = RunResult("trial", RunStatus.SUCCESS, "ok", 3, usage=usage, trace_path=str(trace))
    metrics = extract_metrics(result, wall_time_seconds=1.25)
    assert metrics.steps == 3 and metrics.total_tokens == 25
    assert metrics.tool_call_count == 3 and metrics.test_attempt_count == 1
    assert metrics.completion_rejection_count == 1 and metrics.reflection_count == 1
    assert metrics.file_read_count == 1 and metrics.shell_call_count == 1
    assert metrics.plan_created_count == 1 and metrics.plan_revision_count == 1
    assert metrics.plan_step_completed_count == 1 and metrics.planning_skipped is True
    assert metrics.failure_classified_count == 1
    assert metrics.recovery_selected_count == 1
    assert metrics.recovery_replan_count == 1
    assert metrics.recovery_exhausted_count == 1


def test_agent_failure_still_emits_trial_result(tmp_path: Path):
    suite = EvaluationSuite("suite", (_task(),), defaults={"max_steps": 3, "budget_tokens": 1000})

    def factory(task, repo, trace_dir, trial_id):
        backend = MockBackend([Action(ActionType.GIVE_UP, thought="stop", message="cannot solve")])
        registry = ToolRegistry().register(FileWriteTool(workspace=str(repo)))
        return ExecutionRunner(backend=backend, registry=registry, config=AgentConfig(max_steps=3, repo_map_mode="none"), log_dir=str(trace_dir))

    results = EvaluationHarness(
        suite=suite, output_dir=tmp_path / "out", runner_factory=factory,
        evidence_kind="deterministic_harness", real_model_executed=False,
    ).run()
    assert len(results) == 1 and results[0].run_status == "gave_up"
    assert results[0].success is False
    assert (tmp_path / "out" / "trials" / results[0].trial_id / "trial.json").is_file()


def test_fake_backend_success_is_not_reported_as_capability_rate(tmp_path: Path):
    suite = EvaluationSuite("suite", (_task(),), defaults={"max_steps": 4, "budget_tokens": 1000})

    def factory(task, repo, trace_dir, trial_id):
        backend = MockBackend([
            Action(ActionType.TOOL_CALL, thought="edit", tool_call=ToolCall("file_write", {"path": "value.txt", "content": "done\n"})),
            Action(ActionType.FINISH, thought="done", message="done"),
        ])
        registry = ToolRegistry().register(FileWriteTool(workspace=str(repo)))
        return ExecutionRunner(backend=backend, registry=registry, config=AgentConfig(max_steps=4, repo_map_mode="none"), log_dir=str(trace_dir))

    result = EvaluationHarness(
        suite=suite, output_dir=tmp_path / "out", runner_factory=factory,
        evidence_kind="deterministic_harness", real_model_executed=False,
    ).run()[0]
    assert result.success is True
    report = build_report([result], suite_id="suite")
    assert report["real_model_executed"] is False
    assert report["harness_validation"]["pass_rate_intentionally_omitted"] is True
    assert "real_model_small_sample" not in report


def test_report_aggregation_for_real_model_rows_only():
    def row(repetition: int, success: bool, steps: int) -> TrialResult:
        return TrialResult(
            suite_id="suite", task_id="tiny-task",
            trial_id=f"tiny-task--baseline_react--r{repetition:03d}",
            variant="baseline_react", repetition=repetition, execution_status="executed",
            evidence_kind="real_model", real_model_executed=True, run_status="success",
            termination_reason="completion_satisfied", acceptance_status="passed",
            success=success, metrics=TrialMetrics(steps=steps, total_tokens=10 * repetition),
            grader_results=(GraderResult("g", "file", success, True, "fixture"),),
        )

    report = build_report([row(1, True, 2), row(2, False, 4)], suite_id="suite")
    aggregate = report["real_model_small_sample"]["baseline_react"]
    assert report["real_model_executed"] is True
    assert aggregate["runs"] == 2 and aggregate["observed_successes"] == 1
    assert aggregate["observed_success_rate"] == 0.5 and aggregate["mean_steps"] == 3


def test_output_directory_refuses_silent_overwrite(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        reserve_output_dir(existing)


def test_no_provider_artifact_is_explicit_not_executed(tmp_path: Path):
    suite = EvaluationSuite("suite", (_task(),))
    report = write_not_executed(suite, tmp_path / "out", reason="no_credentials")
    assert report["execution_status"] == "not_executed"
    assert report["real_model_executed"] is False and report["trial_count"] == 0
    assert report["planned_trial_count"] == 1
    metadata = json.loads((tmp_path / "out" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["reason"] == "no_credentials"


def test_not_executed_artifact_respects_task_selection(tmp_path: Path):
    other = _task(
        id="other-task", description="Change other.txt.", files={"other.txt": "old\n"},
        reference_files={"other.txt": "new\n"},
        graders=[{"id": "other", "kind": "file", "params": {"path": "other.txt", "equals": "new\n"}}],
    )
    suite = EvaluationSuite("suite", (_task(), other))
    report = write_not_executed(
        suite, tmp_path / "out", repetitions=3, task_ids=("other-task",), reason="no_credentials"
    )
    assert report["tasks"] == ["other-task"]
    assert report["planned_trial_count"] == 3


def test_reference_solutions_self_check_for_frozen_suite(tmp_path: Path):
    suite = EvaluationSuite.load(FIXTURE)
    assert len(suite.tasks) == 8
    result = validate_suite_references(suite, tmp_path / "references")
    assert set(result) == {task.task_id for task in suite.tasks}
    assert all(result[task.task_id] for task in suite.tasks)
