"""Typed artifacts for offline trajectory-driven Skill evolution."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_id(value: str, *, field_name: str) -> None:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must match {_ID_RE.pattern}: {value!r}")


def _validate_sha256(value: str, *, field_name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase sha256 hex digest")


class PatternType(str, Enum):
    SUCCESSFUL_WORKFLOW = "successful_workflow"
    RECOVERY_WORKFLOW = "recovery_workflow"


class CandidateStatus(str, Enum):
    DRAFT = "draft"
    EVALUATING = "evaluating"
    REJECTED = "rejected"
    APPROVED = "approved"
    PROMOTED = "promoted"


class EvaluationRole(str, Enum):
    TARGET = "target"
    SHOULD_TRIGGER = "should_trigger"
    SHOULD_NOT_TRIGGER = "should_not_trigger"
    NON_REGRESSION = "non_regression"


class PromotionStatus(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EVALUATION_FAILED = "evaluation_failed"


@dataclass(frozen=True)
class TrajectoryRef:
    run_id: str
    task_id: str
    trace_ref: str
    trace_sha256: str
    run_status: str
    acceptance_status: str
    termination_reason: str | None = None
    suite_id: str | None = None
    trial_id: str | None = None
    eval_task_id: str | None = None
    trial_result_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.trace_ref:
            raise ValueError("trajectory provenance requires run_id, task_id and trace_ref")
        _validate_sha256(self.trace_sha256, field_name="trace_sha256")

    def identity_dict(self) -> dict[str, Any]:
        """Stable provenance identity; local artifact paths are deliberately excluded."""
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trace_sha256": self.trace_sha256,
            "run_status": self.run_status,
            "acceptance_status": self.acceptance_status,
            "termination_reason": self.termination_reason,
            "suite_id": self.suite_id,
            "trial_id": self.trial_id,
            "eval_task_id": self.eval_task_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrajectoryRef":
        return cls(**raw)


@dataclass(frozen=True)
class NormalizedTrajectory:
    ref: TrajectoryRef
    workflow: tuple[str, ...]
    failure_categories: tuple[str, ...] = ()
    recovery_strategies: tuple[str, ...] = ()
    loaded_skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    eligible: bool = False
    eligibility_reason: str = ""

    @property
    def has_recovery(self) -> bool:
        return bool(self.failure_categories and self.recovery_strategies)


@dataclass(frozen=True)
class ExperiencePattern:
    pattern_id: str
    pattern_type: PatternType
    signature: tuple[str, ...]
    source_trajectories: tuple[TrajectoryRef, ...]
    failure_categories: tuple[str, ...] = ()
    recovery_strategies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.pattern_id, field_name="pattern_id")
        if not self.signature:
            raise ValueError("experience pattern requires a non-empty signature")
        if not self.source_trajectories:
            raise ValueError("experience pattern requires source trajectories")

    @property
    def evidence_count(self) -> int:
        return len(self.source_trajectories)

    @classmethod
    def build(
        cls,
        *,
        pattern_type: PatternType,
        signature: tuple[str, ...],
        source_trajectories: tuple[TrajectoryRef, ...],
        failure_categories: tuple[str, ...] = (),
        recovery_strategies: tuple[str, ...] = (),
    ) -> "ExperiencePattern":
        unique = {
            (item.run_id, item.task_id, item.trace_sha256, item.trial_id): item
            for item in source_trajectories
        }
        ordered = tuple(sorted(
            unique.values(),
            key=lambda item: (
                item.run_id,
                item.task_id,
                item.trace_sha256,
                item.trial_id or "",
            ),
        ))
        digest = stable_hash({
            "pattern_type": pattern_type.value,
            "signature": list(signature),
            "sources": [item.identity_dict() for item in ordered],
            "failure_categories": list(failure_categories),
            "recovery_strategies": list(recovery_strategies),
        })[:16]
        return cls(
            pattern_id=f"pattern-{digest}",
            pattern_type=pattern_type,
            signature=signature,
            source_trajectories=ordered,
            failure_categories=failure_categories,
            recovery_strategies=recovery_strategies,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "pattern_type": self.pattern_type.value,
            "signature": list(self.signature),
            "source_trajectories": [item.to_dict() for item in self.source_trajectories],
            "failure_categories": list(self.failure_categories),
            "recovery_strategies": list(self.recovery_strategies),
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True)
class SkillCandidate:
    candidate_id: str
    candidate_version: int
    skill_name: str
    description: str
    instructions: str
    pattern_id: str
    pattern_type: PatternType
    source_trajectories: tuple[TrajectoryRef, ...]
    content_hash: str
    created_at: str
    parent_skill_name: str | None = None
    parent_skill_version: int | None = None
    parent_skill_hash: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.candidate_id, field_name="candidate_id")
        _validate_id(self.skill_name, field_name="skill_name")
        if self.candidate_version < 1:
            raise ValueError("candidate_version must be >= 1")
        if not self.description.strip() or len(self.description) > 500:
            raise ValueError("candidate description must be 1..500 characters")
        if not self.instructions.strip():
            raise ValueError("candidate instructions are required")
        if not self.source_trajectories:
            raise ValueError("candidate provenance requires source trajectories")
        _validate_sha256(self.content_hash, field_name="content_hash")
        if self.parent_skill_hash is not None:
            _validate_sha256(self.parent_skill_hash, field_name="parent_skill_hash")
        parent_fields = (self.parent_skill_name, self.parent_skill_version, self.parent_skill_hash)
        if any(value is not None for value in parent_fields) and not all(value is not None for value in parent_fields):
            raise ValueError("parent skill provenance must be complete")

    @staticmethod
    def semantic_hash(skill_name: str, description: str, instructions: str) -> str:
        return stable_hash({
            "name": skill_name,
            "description": " ".join(description.split()),
            "instructions": instructions.strip(),
        })

    @classmethod
    def build(
        cls,
        *,
        skill_name: str,
        description: str,
        instructions: str,
        pattern: ExperiencePattern,
        parent_skill_name: str | None = None,
        parent_skill_version: int | None = None,
        parent_skill_hash: str | None = None,
        created_at: str | None = None,
    ) -> "SkillCandidate":
        content_hash = cls.semantic_hash(skill_name, description, instructions)
        version = (int(parent_skill_version) + 1) if parent_skill_version is not None else 1
        digest = stable_hash({
            "skill_name": skill_name,
            "candidate_version": version,
            "pattern_id": pattern.pattern_id,
            "content_hash": content_hash,
            "sources": [item.identity_dict() for item in pattern.source_trajectories],
            "parent_skill_hash": parent_skill_hash,
        })[:16]
        return cls(
            candidate_id=f"candidate-{digest}",
            candidate_version=version,
            skill_name=skill_name,
            description=" ".join(description.split()),
            instructions=instructions.strip(),
            pattern_id=pattern.pattern_id,
            pattern_type=pattern.pattern_type,
            source_trajectories=pattern.source_trajectories,
            content_hash=content_hash,
            created_at=created_at or utc_now(),
            parent_skill_name=parent_skill_name,
            parent_skill_version=parent_skill_version,
            parent_skill_hash=parent_skill_hash,
        )

    def skill_markdown(self) -> str:
        return (
            "---\n"
            f"name: {self.skill_name}\n"
            f"description: {self.description}\n"
            "---\n"
            f"{self.instructions.strip()}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["pattern_type"] = self.pattern_type.value
        payload["source_trajectories"] = [item.to_dict() for item in self.source_trajectories]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SkillCandidate":
        data = dict(raw)
        data["pattern_type"] = PatternType(str(data["pattern_type"]))
        data["source_trajectories"] = tuple(TrajectoryRef.from_dict(item) for item in data["source_trajectories"])
        return cls(**data)


@dataclass(frozen=True)
class EvaluationCase:
    task_id: str
    repetition: int
    role: EvaluationRole
    baseline_success: bool
    candidate_success: bool
    baseline_required_graders_passed: bool
    candidate_required_graders_passed: bool
    candidate_loaded: bool
    candidate_process_passed: bool
    baseline_steps: int
    candidate_steps: int
    baseline_tokens: int
    candidate_tokens: int
    evaluation_failed: bool = False
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role"] = self.role.value
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvaluationCase":
        data = dict(raw)
        data["role"] = EvaluationRole(str(data["role"]))
        return cls(**data)


@dataclass(frozen=True)
class EvaluationRecord:
    record_id: str
    candidate_id: str
    candidate_version: int
    candidate_hash: str
    suite_id: str
    baseline_variant: str
    candidate_variant: str
    cases: tuple[EvaluationCase, ...]
    created_at: str

    def __post_init__(self) -> None:
        _validate_id(self.record_id, field_name="record_id")
        _validate_sha256(self.candidate_hash, field_name="candidate_hash")
        if not self.cases:
            raise ValueError("evaluation record requires at least one case")

    @classmethod
    def build(
        cls,
        *,
        candidate: SkillCandidate,
        suite_id: str,
        baseline_variant: str,
        candidate_variant: str,
        cases: tuple[EvaluationCase, ...],
        created_at: str | None = None,
    ) -> "EvaluationRecord":
        ordered = tuple(sorted(cases, key=lambda case: (case.task_id, case.repetition, case.role.value)))
        digest = stable_hash({
            "candidate_id": candidate.candidate_id,
            "candidate_version": candidate.candidate_version,
            "candidate_hash": candidate.content_hash,
            "suite_id": suite_id,
            "baseline_variant": baseline_variant,
            "candidate_variant": candidate_variant,
            "cases": [case.to_dict() for case in ordered],
        })[:16]
        return cls(
            record_id=f"evaluation-{digest}",
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            candidate_hash=candidate.content_hash,
            suite_id=suite_id,
            baseline_variant=baseline_variant,
            candidate_variant=candidate_variant,
            cases=ordered,
            created_at=created_at or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cases"] = [case.to_dict() for case in self.cases]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvaluationRecord":
        data = dict(raw)
        data["cases"] = tuple(EvaluationCase.from_dict(item) for item in data["cases"])
        return cls(**data)


@dataclass(frozen=True)
class PromotionDecision:
    decision_id: str
    status: PromotionStatus
    candidate_id: str
    candidate_version: int
    candidate_hash: str
    evaluation_record_id: str
    reasons: tuple[str, ...]
    gate_config: dict[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        _validate_id(self.decision_id, field_name="decision_id")
        _validate_sha256(self.candidate_hash, field_name="candidate_hash")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["reasons"] = list(self.reasons)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PromotionDecision":
        data = dict(raw)
        data["status"] = PromotionStatus(str(data["status"]))
        data["reasons"] = tuple(str(item) for item in data.get("reasons", ()))
        return cls(**data)
