"""S08 offline contract tests: NO LLM, no Docker, no Forge production imports."""
import pytest
from evals.completion_candidate_probe import (
    CheckEvidence,
    CheckOrigin,
    CheckOutcome,
    EvidenceSnapshot,
    Readiness,
    evaluate_completion_candidate,
)


def evidence(check_id="focused", outcome=CheckOutcome.PASSED, *,
             origin=CheckOrigin.REPOSITORY, snapshot="v2", sequence=1):
    return CheckEvidence(check_id, origin, outcome, snapshot, sequence)


def check(**changes):
    base = dict(
        initial_content_fingerprint="v1", current_content_fingerprint="v2",
        checks=(evidence(),),
    )
    base.update(changes)
    return evaluate_completion_candidate(EvidenceSnapshot(**base))


def test_s08_01_no_change_cannot_be_ready():
    assert check(current_content_fingerprint="v1").state is Readiness.NOT_READY


def test_s08_02_passed_existing_test_makes_candidate_only():
    decision = check()
    assert decision.state is Readiness.CANDIDATE_READY
    assert not hasattr(decision, "status")  # cannot turn into Agent SUCCESS


def test_s08_03_reedit_after_test_stales_verification():
    assert check(current_content_fingerprint="v3").state is Readiness.NOT_READY


def test_s08_04_pilot_repro_file_requires_review():
    decision = check(untracked_paths=("repro_issue.py",))
    assert decision.state is Readiness.WRAP_UP_REQUIRED
    assert "repro_issue.py" in " ".join(decision.reasons)


def test_s08_05_diagnostic_failure_after_focused_pass_not_dropped():
    decision = check(checks=(
        evidence(),
        evidence("tmp", CheckOutcome.FAILED, origin=CheckOrigin.DIAGNOSTIC, sequence=2),
    ))
    assert decision.state is Readiness.WRAP_UP_REQUIRED


def test_s08_06_model_selected_test_pass_cannot_replace_repo_regression():
    assert check(
        checks=(evidence("model_created", origin=CheckOrigin.DIAGNOSTIC),)
    ).state is Readiness.NOT_READY


def test_s08_07_required_failed_is_not_overridden_by_diagnostic_success():
    decision = check(
        required_check_ids=("contract",),
        checks=(
            evidence("contract", CheckOutcome.FAILED, origin=CheckOrigin.CONTRACT),
            evidence("tmp", CheckOutcome.PASSED, origin=CheckOrigin.DIAGNOSTIC, sequence=2),
        ),
    )
    assert decision.state is Readiness.NOT_READY


@pytest.mark.parametrize("outcome", [CheckOutcome.TIMEOUT, CheckOutcome.INFRASTRUCTURE])
def test_s08_08_timeout_and_infra_not_passing(outcome):
    assert check(checks=(evidence(outcome=outcome),)).state is Readiness.NOT_READY


def test_s08_09_git_commit_only_no_content_change_is_fresh():
    assert check().state is Readiness.CANDIDATE_READY


def test_s08_10_explicit_readonly_no_test_required():
    decision = check(
        require_changes=False, require_tests=False,
        current_content_fingerprint="v1", checks=(),
    )
    assert decision.state is Readiness.CANDIDATE_READY


def test_s08_11_unresolved_work_and_infra_block():
    assert check(unresolved_required_work=True).state is Readiness.NOT_READY
    assert check(fatal_infrastructure_error=True).state is Readiness.NOT_READY


def test_s08_12_untracked_intentional_new_source_must_be_reviewed_or_allowed():
    assert check(untracked_paths=("new_feature.py",)).state is Readiness.WRAP_UP_REQUIRED
    assert check(
        untracked_paths=("new_feature.py",),
        explicitly_allowed_new_paths=("new_feature.py",),
    ).state is Readiness.CANDIDATE_READY


def test_s08_13_required_test_snapshot_binding():
    decision = check(
        required_check_ids=("required",),
        checks=(
            evidence("required", origin=CheckOrigin.CONTRACT, snapshot="v1"),
            evidence("focused", origin=CheckOrigin.REPOSITORY, snapshot="v2"),
        ),
    )
    assert decision.state is Readiness.NOT_READY


def test_repeating_same_state_is_pure_and_has_no_feedback_loop():
    snapshot = EvidenceSnapshot("v1", "v2", checks=(evidence(),))
    result = evaluate_completion_candidate(snapshot)
    assert [evaluate_completion_candidate(snapshot) for _ in range(20)] == [result] * 20


def test_later_success_of_same_diagnostic_test_resolves_it():
    decision = check(checks=(
        evidence(),
        evidence("tmp", CheckOutcome.FAILED, origin=CheckOrigin.DIAGNOSTIC, sequence=2),
        evidence("tmp", CheckOutcome.PASSED, origin=CheckOrigin.DIAGNOSTIC, sequence=3),
    ))
    assert decision.state is Readiness.CANDIDATE_READY


def test_failed_diagnostic_from_old_snapshot_not_applied_to_new_patch():
    decision = check(checks=(
        evidence(),
        evidence("tmp", CheckOutcome.FAILED, origin=CheckOrigin.DIAGNOSTIC, snapshot="v1"),
    ))
    assert decision.state is Readiness.CANDIDATE_READY
