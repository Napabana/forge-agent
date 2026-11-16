"""可信宿主评分与阶段异常合同；没有 Provider 请求。"""
import json
from dataclasses import replace

import pytest

from agent.task import RunResult, RunStatus
from evals.coding_agent.runner import EvaluationHarness
from evals.coding_agent.report import build_report
from evals.coding_agent.schema import EvalTask, EvaluationSuite, GraderSpec


def suite():
    return EvaluationSuite("score", (EvalTask(
        "tiny", "inspect fixture", {"value.txt": "ok"},
        (GraderSpec("exists", "file", {"path": "value.txt", "equals": "ok"}),),
        require_changes=False,
    ),))


def run_trial(tmp_path, *, status=RunStatus.SUCCESS, reason="completion_satisfied",
              pass_verifier=True, close_error=False, trace=True, usage=True):
    def factory(task, repo, traces, trial_id):
        class Runner:
            def run(self, request):
                if not pass_verifier:
                    (repo / "value.txt").write_text("bad")
                trace_path = None
                if trace:
                    traces.mkdir(parents=True)
                    path = traces / "run.jsonl"
                    payload = {"run_id": "r", "model_call_id": "c", "step_id": 1}
                    tokens = dict(input_tokens=3, output_tokens=2, cached_tokens=0,
                                  reasoning_tokens=1, total_tokens=5) if usage else {}
                    events = [
                        dict(event_type="llm_call_started", payload=payload),
                        dict(event_type="llm_call_finished", payload={**payload, "provider_usage": tokens}),
                        dict(event_type="run_terminated", payload={"run_id": "r", "status": status.value}),
                    ]
                    path.write_text("\n".join(json.dumps(event) for event in events))
                    trace_path = str(path)
                return RunResult(trial_id, status, "result", 1, trace_path=trace_path,
                                 termination_reason=reason)

            def close(self):
                if close_error:
                    raise OSError("cleanup failed")
        return Runner()
    return EvaluationHarness(suite=suite(), output_dir=tmp_path / "out",
                             runner_factory=factory, evidence_kind="real_model",
                             real_model_executed=True).run()[0]


@pytest.mark.parametrize("status,passed,expected", [
    (RunStatus.SUCCESS, True, True), (RunStatus.SUCCESS, False, False),
    (RunStatus.INCOMPLETE, True, True), (RunStatus.GAVE_UP, False, False),
])
def test_snapshot_score_is_independent_of_natural_finish(tmp_path, status, passed, expected):
    row = run_trial(tmp_path, status=status, pass_verifier=passed)
    assert row.scoreable is True and row.scoreable_success is expected
    assert row.success is (status == RunStatus.SUCCESS and passed)
    assert row.model_call_started is True


@pytest.mark.parametrize("reason,domain", [
    ("provider_error", "provider"), ("infrastructure_error", "harness"),
    ("canceled", "cancellation"),
])
def test_structured_failure_excluded_even_with_passing_snapshot(tmp_path, reason, domain):
    row = run_trial(tmp_path, status=RunStatus.FAILED, reason=reason)
    assert row.failure_domain == domain and row.scoreable is False
    assert row.verifier_status == "passed"
    aggregate = build_report([row], suite_id="score")["real_model_small_sample"]["baseline_react"]
    assert aggregate["observed_success_rate"] is None
    assert aggregate["raw_verifier_passes"] == 1


def test_cleanup_does_not_overwrite_success_or_verifier(tmp_path):
    row = run_trial(tmp_path, close_error=True)
    assert row.run_status == "success" and row.scoreable_success is True
    assert row.phase_errors[0]["phase"] == "cleanup"
    assert row.verifier_status == "passed"


def test_no_call_is_not_real_execution(tmp_path):
    row = run_trial(tmp_path, trace=False)
    assert row.real_model_executed is False and row.model_call_started is False
    assert row.scoreability_reason == "no_model_call_started"
    assert row.usage_complete is None


def test_missing_usage_does_not_become_zero_cost(tmp_path):
    row = run_trial(tmp_path, usage=False)
    assert row.scoreable is True and row.usage_complete is False
    aggregate = build_report([row], suite_id="score")["real_model_small_sample"]["baseline_react"]
    assert aggregate["mean_total_tokens"] is None


def test_materialize_error_and_grader_error_are_retained(tmp_path, monkeypatch):
    import evals.coding_agent.runner as module
    monkeypatch.setattr(module, "materialize_task_repo", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    row = run_trial(tmp_path)
    assert row.failure_domain == "task_environment"
    assert row.phase_errors[0]["phase"] == "materialize"
    assert row.final_state_ref is None


def test_grader_exception_preserves_run_result(tmp_path, monkeypatch):
    import evals.coding_agent.runner as module
    monkeypatch.setattr(module, "grade_many", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("grader")))
    row = run_trial(tmp_path)
    assert row.run_status == "success" and row.verifier_status == "error"
    assert row.scoreable is False and row.failure_domain == "harness"


def test_legacy_reports_are_versioned_separately(tmp_path):
    row = run_trial(tmp_path)
    legacy = replace(row, schema_version=1, trial_id="legacy")
    report = build_report([row, legacy], suite_id="score")
    assert report["schema_version"] == 2
    assert report["legacy_v1"]["schema_version"] == 1
    assert report["scoreability"]["total_trials"] == 1


def test_different_policies_cannot_silently_mix(tmp_path):
    row = run_trial(tmp_path)
    other = replace(row, trial_id="other", scoring_policy_fingerprint="different")
    with pytest.raises(ValueError, match="policy"):
        build_report([row, other], suite_id="score")


def test_command_grader_cannot_execute_is_error_not_model_failure(tmp_path):
    from evals.coding_agent.graders import GraderContext, grade
    result = grade(GraderSpec("missing", "command", {"command": ["/forge-no-such-executable"]}),
                   GraderContext(tmp_path))
    assert result.passed is False and result.evidence["verifier_error"] is True


def test_unreadable_trace_still_writes_trial(tmp_path, monkeypatch):
    import evals.coding_agent.runner as module
    def broken_metrics(result, **kwargs):
        raise UnicodeError("invalid trace encoding")

    monkeypatch.setattr(module, "extract_metrics", broken_metrics)
    row = run_trial(tmp_path)
    assert row.scoreable is False and row.failure_domain == "harness"
    assert any(error["phase"] == "metrics" for error in row.phase_errors)
    assert (tmp_path / "out" / "trials" / row.trial_id / "trial.json").is_file()


def test_external_harbor_timeout_keeps_reward_with_frozen_policy(tmp_path):
    from examples.harbor.trace_evidence import audit_trial
    from evals.trial_outcome_probe import FailureDomain, ScoringPolicy
    trace = tmp_path / "trace.jsonl"
    payload = dict(run_id="run", model_call_id="c", step_id=1)
    trace.write_text(json.dumps(dict(event_type="llm_call_started", payload=payload)) + "\n")
    harbor = tmp_path / "harbor.json"
    harbor.write_text(json.dumps(dict(exception_info={"exception_type": "AgentTimeoutError"},
                                     verifier_result={"rewards": {"reward": 1.0}})))
    policy = ScoringPolicy("deadline-snapshot")
    actual = audit_trial(trace, harbor, scoring_policy=policy,
                         failure_domain=FailureDomain.NONE, snapshot_within_deadline=True)
    assert actual["scoreable_success"] is True and actual["natural_finish"] is False
    assert actual["trace"]["usage_is_lower_bound"] is True
    excluded = audit_trial(trace, harbor, scoring_policy=ScoringPolicy("natural", grade_timeout_snapshots=False),
                           failure_domain=FailureDomain.NONE, snapshot_within_deadline=True)
    assert excluded["scoreable"] is False
    assert excluded["harbor"]["verifier_reward"] == 1.0
    with pytest.raises(ValueError, match="explicit"):
        audit_trial(trace, harbor, scoring_policy=policy)
