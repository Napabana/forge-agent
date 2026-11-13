"""Experimental, offline-only completion-evidence model.

This module is NOT wired into Agent.run, Runner, Harbor, or GitHub delivery.
It never returns RunStatus.SUCCESS. It is a deterministic design probe for
Stage 08's proposed CompletionCandidate / TestEvidence semantics.

All evidence labeled trusted must originate from the host/task contract,
not from an LLM-controlled tool argument or self-reported string.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Readiness(str, Enum):
    NOT_READY = "not_ready"
    WRAP_UP_REQUIRED = "wrap_up_required"
    CANDIDATE_READY = "candidate_ready"


class CheckOrigin(str, Enum):
    CONTRACT = "contract"
    REPOSITORY = "repository"
    DIAGNOSTIC = "diagnostic"


class CheckOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class CheckEvidence:
    check_id: str
    origin: CheckOrigin
    outcome: CheckOutcome
    repo_content_fingerprint: str
    sequence: int


@dataclass(frozen=True)
class EvidenceSnapshot:
    initial_content_fingerprint: str
    current_content_fingerprint: str
    require_changes: bool = True
    require_tests: bool = True
    required_check_ids: tuple[str, ...] = ()
    checks: tuple[CheckEvidence, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    explicitly_allowed_new_paths: tuple[str, ...] = ()
    unresolved_required_work: bool = False
    fatal_infrastructure_error: bool = False


@dataclass(frozen=True)
class ReadinessDecision:
    state: Readiness
    reasons: tuple[str, ...]


def evaluate_completion_candidate(snapshot: EvidenceSnapshot) -> ReadinessDecision:
    """Evaluate candidate readiness, never task correctness or SUCCESS.

    Caller must construct trustworthy test origin and task requirements.
    A caller cannot make a diagnostic test into a contract test merely by
    labeling LLM arguments; this prototype does not authenticate provenance.
    """
    blockers: list[str] = []
    cleanup: list[str] = []
    if snapshot.fatal_infrastructure_error:
        blockers.append("unresolved infrastructure error")
    if snapshot.unresolved_required_work:
        blockers.append("unresolved task requirements")
    if snapshot.require_changes and (
        snapshot.current_content_fingerprint == snapshot.initial_content_fingerprint
    ):
        blockers.append("repository has no effective content change")

    # A test result belongs to the exact content snapshot on which it ran.
    current = [
        check for check in snapshot.checks
        if check.repo_content_fingerprint == snapshot.current_content_fingerprint
    ]
    # A later successful rerun supersedes earlier failure of the same check.
    latest: dict[str, CheckEvidence] = {}
    for check in current:
        old = latest.get(check.check_id)
        if old is None or check.sequence > old.sequence:
            latest[check.check_id] = check

    for required_id in snapshot.required_check_ids:
        check = latest.get(required_id)
        if not check or check.origin is not CheckOrigin.CONTRACT:
            blockers.append(f"trusted required check missing/stale: {required_id}")
        elif check.outcome is not CheckOutcome.PASSED:
            blockers.append(f"trusted required check did not pass: {required_id}")

    if snapshot.require_tests and not snapshot.required_check_ids:
        # Model-selected, newly created tests cannot establish task success.
        passed_existing = any(
            check.outcome is CheckOutcome.PASSED
            and check.origin in (CheckOrigin.CONTRACT, CheckOrigin.REPOSITORY)
            for check in latest.values()
        )
        if not passed_existing:
            blockers.append("no passing trusted/repository test for current snapshot")

    for check in latest.values():
        if check.outcome in (CheckOutcome.FAILED, CheckOutcome.TIMEOUT, CheckOutcome.INFRASTRUCTURE):
            if check.origin is CheckOrigin.CONTRACT:
                blockers.append(f"unresolved contract check: {check.check_id}")
            else:
                cleanup.append(f"review unresolved {check.origin.value} check: {check.check_id}")

    # New files can be intended deliverables. Never delete/ignore automatically.
    allowed = set(snapshot.explicitly_allowed_new_paths)
    undecided_files = sorted(set(snapshot.untracked_paths) - allowed)
    if undecided_files:
        cleanup.append("unclassified new paths: " + ", ".join(undecided_files))

    if blockers:
        return ReadinessDecision(Readiness.NOT_READY, tuple(dict.fromkeys(blockers + cleanup)))
    if cleanup:
        return ReadinessDecision(Readiness.WRAP_UP_REQUIRED, tuple(dict.fromkeys(cleanup)))
    return ReadinessDecision(Readiness.CANDIDATE_READY, ())
