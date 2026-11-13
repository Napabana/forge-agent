"""Stage 09: offline-only scoreability and outcome contract probes."""
import pytest

from evals.trial_outcome_probe import (
    FailureDomain, RolloutStatus, ScoringPolicy, TrialEvidence,
    VerifierStatus, decide_trial, summarize_trials,
)

POLICY = ScoringPolicy("harbor-final-snapshot-probe-v1")


def trial(**overrides):
    fields = dict(
        trial_id="t1", model_call_started=True,
        rollout_status=RolloutStatus.FINISH,
        failure_domain=FailureDomain.NONE,
        verifier_status=VerifierStatus.PASSED,
        snapshot_available=True,
        snapshot_within_deadline=True,
    )
    fields.update(overrides)
    return TrialEvidence(**fields)


def test_e09_01_provider_error_with_no_grade_not_scoreable():
    d = decide_trial(trial(failure_domain=FailureDomain.PROVIDER,
                           rollout_status=RolloutStatus.FAILED,
                           verifier_status=VerifierStatus.NOT_RUN), POLICY)
    assert not d.scoreable and d.scoreable_success is None
    assert d.scoreability_reason == "non_model_failure:provider"


def test_e09_02_runner_setup_failure_does_not_count_as_model_trial():
    d = decide_trial(trial(model_call_started=False, failure_domain=FailureDomain.HARNESS), POLICY)
    assert not d.scoreable and not d.natural_finish
    assert d.verifier_passed is True  # preserve raw, even inconsistent, evidence


def test_e09_03_no_materialized_snapshot_not_scoreable():
    d = decide_trial(trial(snapshot_available=False), POLICY)
    assert d.scoreability_reason == "snapshot_unavailable"


def test_e09_04_incomplete_with_grader_pass_preserves_both_axes():
    d = decide_trial(trial(rollout_status=RolloutStatus.INCOMPLETE,
                           failure_domain=FailureDomain.MODEL), POLICY)
    assert d.scoreable_success is True and not d.natural_finish


def test_e09_05_natural_finish_but_verifier_fail_is_failure():
    d = decide_trial(trial(verifier_status=VerifierStatus.FAILED), POLICY)
    assert d.natural_finish and d.scoreable and d.scoreable_success is False


def test_e09_06_pilot_timeout_reward_one_is_explicitly_scoreable_under_policy():
    d = decide_trial(trial(rollout_status=RolloutStatus.TIMEOUT,
                           failure_domain=FailureDomain.MODEL), POLICY)
    assert d.verifier_passed is True
    assert d.scoreable_success is True and not d.natural_finish


def test_e09_06b_timeout_can_be_excluded_by_frozen_policy():
    policy = ScoringPolicy("natural-finish-v1", grade_timeout_snapshots=False)
    d = decide_trial(trial(rollout_status=RolloutStatus.TIMEOUT,
                           failure_domain=FailureDomain.MODEL), policy)
    assert d.verifier_passed is True and d.scoreable_success is None
    assert d.scoreability_reason == "timeout_excluded_by_policy"


def test_e09_07_cleanup_failure_preserved_as_harness_failure_not_model():
    d = decide_trial(trial(failure_domain=FailureDomain.HARNESS), POLICY)
    assert not d.scoreable and d.scoreability_reason == "non_model_failure:harness"


def test_e09_08_no_real_model_request_disqualifies_fake_trial():
    d = decide_trial(trial(model_call_started=False), POLICY)
    assert not d.scoreable


def test_e09_09_outside_budget_snapshot_not_graded_even_if_verifier_passed():
    d = decide_trial(trial(snapshot_within_deadline=False), POLICY)
    assert d.verifier_passed is True and not d.scoreable


@pytest.mark.parametrize("status", [VerifierStatus.NOT_RUN, VerifierStatus.ERROR])
def test_e09_10_missing_or_failed_grader_not_scoreable(status):
    d = decide_trial(trial(verifier_status=status), POLICY)
    assert d.scoreable_success is None and d.verifier_passed is None


def test_summary_retains_total_and_unscoreable_denominators():
    decisions = [
        decide_trial(trial(trial_id="a"), POLICY),
        decide_trial(trial(trial_id="b", rollout_status=RolloutStatus.TIMEOUT,
                           failure_domain=FailureDomain.MODEL), POLICY),
        decide_trial(trial(trial_id="c", failure_domain=FailureDomain.PROVIDER,
                           verifier_status=VerifierStatus.NOT_RUN), POLICY),
        decide_trial(trial(trial_id="d", verifier_status=VerifierStatus.FAILED), POLICY),
    ]
    report = summarize_trials(decisions, POLICY)
    assert report["total_trials"] == 4
    assert report["scoreable_trials"] == 3
    assert report["scoreable_successes"] == 2
    assert report["scoreable_success_rate"] == 2 / 3
    assert report["natural_finishes"] == 3
    assert report["unscoreable_reasons"]["non_model_failure:provider"] == 1


def test_empty_aggregation_not_reported_as_zero_percent_success():
    report = summarize_trials([], POLICY)
    assert report["total_trials"] == 0
    assert report["scoreable_success_rate"] is None


def test_duplicate_trial_ids_cannot_be_double_counted():
    d = decide_trial(trial(), POLICY)
    with pytest.raises(ValueError, match="duplicate trial_id"):
        summarize_trials([d, d], POLICY)


def test_incomplete_can_be_excluded_without_discarding_verifier_result():
    policy = ScoringPolicy("natural-finish-v2", grade_incomplete_snapshots=False)
    d = decide_trial(trial(rollout_status=RolloutStatus.INCOMPLETE), policy)
    assert not d.scoreable and d.verifier_passed is True


def test_gave_up_with_valid_snapshot_can_be_scored_by_policy():
    d = decide_trial(trial(rollout_status=RolloutStatus.GAVE_UP,
                           failure_domain=FailureDomain.MODEL), POLICY)
    assert d.scoreable and d.scoreable_success


def test_cancelled_trial_not_scoreable_by_default():
    d = decide_trial(trial(rollout_status=RolloutStatus.CANCELED,
                           failure_domain=FailureDomain.CANCELLATION), POLICY)
    assert not d.scoreable


def test_unknown_failure_domain_not_silently_treated_as_model_failure():
    d = decide_trial(trial(rollout_status=RolloutStatus.FAILED,
                           failure_domain=FailureDomain.UNKNOWN), POLICY)
    assert not d.scoreable


def test_summary_rejects_changed_scoring_policy_after_trial_decision():
    old_policy = ScoringPolicy("fixed-id", grade_timeout_snapshots=True)
    changed_policy = ScoringPolicy("fixed-id", grade_timeout_snapshots=False)
    decision = decide_trial(trial(rollout_status=RolloutStatus.TIMEOUT), old_policy)
    with pytest.raises(ValueError, match="different scoring policies"):
        summarize_trials([decision], changed_policy)


def test_same_policy_settings_have_stable_fingerprint():
    a = ScoringPolicy("freeze-v1")
    b = ScoringPolicy("freeze-v1")
    c = ScoringPolicy("freeze-v1", grade_incomplete_snapshots=False)
    assert a.fingerprint == b.fingerprint and a.fingerprint != c.fingerprint
