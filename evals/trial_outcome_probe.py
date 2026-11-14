"""Offline scoring-contract probe, NOT wired into EvaluationHarness or Harbor.

Evidence and failure domains must be assigned by a trusted evaluator, never
inferred from model text or regex matching exception strings.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable


class RolloutStatus(str, Enum):
    FINISH = "finish"
    INCOMPLETE = "incomplete"
    GAVE_UP = "gave_up"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class FailureDomain(str, Enum):
    NONE = "none"
    MODEL = "model"
    PROVIDER = "provider"
    TASK_ENVIRONMENT = "task_environment"
    HARNESS = "harness"
    CANCELLATION = "cancellation"
    UNKNOWN = "unknown"


class VerifierStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    ERROR = "error"


@dataclass(frozen=True)
class ScoringPolicy:
    """A named scoring policy frozen before experiment results are inspected."""
    identifier: str
    grade_timeout_snapshots: bool = True
    grade_incomplete_snapshots: bool = True
    grade_give_up_snapshots: bool = True

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("ScoringPolicy.identifier must not be blank")

    @property
    def fingerprint(self) -> str:
        payload = {
            "identifier": self.identifier,
            "grade_timeout_snapshots": self.grade_timeout_snapshots,
            "grade_incomplete_snapshots": self.grade_incomplete_snapshots,
            "grade_give_up_snapshots": self.grade_give_up_snapshots,
        }
        return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrialEvidence:
    trial_id: str
    model_call_started: bool
    rollout_status: RolloutStatus
    failure_domain: FailureDomain
    verifier_status: VerifierStatus
    snapshot_available: bool
    snapshot_within_deadline: bool


@dataclass(frozen=True)
class TrialDecision:
    trial_id: str
    policy_fingerprint: str
    scoreable: bool
    scoreability_reason: str
    verifier_passed: bool | None
    scoreable_success: bool | None
    natural_finish: bool
    failure_domain: FailureDomain
    rollout_status: RolloutStatus


def decide_trial(evidence: TrialEvidence, policy: ScoringPolicy) -> TrialDecision:
    """Preserve raw verifier result even when the trial is not scoreable."""
    verifier_passed = {
        VerifierStatus.PASSED: True,
        VerifierStatus.FAILED: False,
        VerifierStatus.NOT_RUN: None,
        VerifierStatus.ERROR: None,
    }[evidence.verifier_status]
    reason = "scoreable"
    if not evidence.model_call_started:
        reason = "no_model_call_started"
    elif evidence.failure_domain not in (FailureDomain.NONE, FailureDomain.MODEL):
        reason = f"non_model_failure:{evidence.failure_domain.value}"
    elif not evidence.snapshot_available:
        reason = "snapshot_unavailable"
    elif not evidence.snapshot_within_deadline:
        reason = "snapshot_outside_deadline"
    elif evidence.verifier_status is VerifierStatus.NOT_RUN:
        reason = "verifier_not_run"
    elif evidence.verifier_status is VerifierStatus.ERROR:
        reason = "verifier_error"
    elif evidence.rollout_status is RolloutStatus.TIMEOUT and not policy.grade_timeout_snapshots:
        reason = "timeout_excluded_by_policy"
    elif evidence.rollout_status is RolloutStatus.INCOMPLETE and not policy.grade_incomplete_snapshots:
        reason = "incomplete_excluded_by_policy"
    elif evidence.rollout_status is RolloutStatus.GAVE_UP and not policy.grade_give_up_snapshots:
        reason = "give_up_excluded_by_policy"
    elif evidence.rollout_status in (RolloutStatus.CANCELED, RolloutStatus.UNKNOWN):
        reason = "unsupported_rollout_status"
    scoreable = reason == "scoreable"
    return TrialDecision(
        trial_id=evidence.trial_id,
        policy_fingerprint=policy.fingerprint,
        scoreable=scoreable,
        scoreability_reason=reason,
        verifier_passed=verifier_passed,
        scoreable_success=verifier_passed if scoreable else None,
        natural_finish=(
            evidence.model_call_started and evidence.rollout_status is RolloutStatus.FINISH
        ),
        failure_domain=evidence.failure_domain,
        rollout_status=evidence.rollout_status,
    )


def summarize_trials(decisions: Iterable[TrialDecision], policy: ScoringPolicy) -> dict:
    rows = list(decisions)
    if any(row.policy_fingerprint != policy.fingerprint for row in rows):
        raise ValueError("Cannot mix trial decisions from different scoring policies")
    trial_ids = [row.trial_id for row in rows]
    if len(set(trial_ids)) != len(trial_ids):
        raise ValueError("duplicate trial_id: metrics would be double-counted")
    scoreable = [row for row in rows if row.scoreable]
    successes = sum(row.scoreable_success is True for row in scoreable)
    return {
        "policy_id": policy.identifier,
        "policy_fingerprint": policy.fingerprint,
        "total_trials": len(rows),
        "scoreable_trials": len(scoreable),
        "unscoreable_trials": len(rows) - len(scoreable),
        "scoreable_successes": successes,
        "scoreable_success_rate": successes / len(scoreable) if scoreable else None,
        "raw_verifier_passes": sum(row.verifier_passed is True for row in rows),
        "natural_finishes": sum(row.natural_finish for row in rows),
        "failure_domain_counts": dict(
            sorted(Counter(row.failure_domain.value for row in rows).items())
        ),
        "rollout_status_counts": dict(
            sorted(Counter(row.rollout_status.value for row in rows).items())
        ),
        "unscoreable_reasons": dict(sorted(Counter(
            row.scoreability_reason for row in rows if not row.scoreable
        ).items())),
    }
