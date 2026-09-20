"""Structured Planning runtime state for P2-1.

Planning is an internal control surface inside the existing Agent loop. It does not
execute repository tools and it is not a second Agent. Provider adapters already parse
function calls into Action/ToolCall, so plan controls reuse that structured contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from agent.task import EventType, Task
from llm.base import LLMToolSchema

PLAN_CREATE = "plan_create"
PLAN_STEP_UPDATE = "plan_step_update"
PLAN_REVISE = "plan_revise"
PLAN_CONTROL_NAMES = frozenset({PLAN_CREATE, PLAN_STEP_UPDATE, PLAN_REVISE})

_MAX_PLAN_STEPS = 12
_MAX_GOAL_CHARS = 600
_MAX_DESCRIPTION_CHARS = 500
_MAX_VERIFICATION_CHARS = 500
_MAX_TARGETS = 8
_MAX_TARGET_CHARS = 200
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_HINT_RE = re.compile(r"\b(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}\b")


class PlanningMode(str, Enum):
    OFF = "off"
    AUTO = "auto"
    ALWAYS = "always"

    @classmethod
    def parse(cls, value: str | "PlanningMode") -> "PlanningMode":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError("planning_mode must be one of: off, auto, always") from exc


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PlanningDecision:
    mode: PlanningMode
    enabled: bool
    reason: str


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    description: str
    targets: tuple[str, ...] = ()
    verification: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, allow_status: bool) -> "PlanStep":
        if not isinstance(raw, dict):
            raise ValueError("plan step must be an object")
        step_id = str(raw.get("id", "")).strip()
        if not _STEP_ID_RE.fullmatch(step_id):
            raise ValueError(f"invalid plan step id: {step_id!r}")
        description = str(raw.get("description", "")).strip()
        if not description:
            raise ValueError(f"plan step {step_id!r} requires a description")
        if len(description) > _MAX_DESCRIPTION_CHARS:
            raise ValueError(f"plan step {step_id!r} description is too long")
        targets_raw = raw.get("targets", ())
        if not isinstance(targets_raw, (list, tuple)):
            raise ValueError(f"plan step {step_id!r} targets must be an array")
        if len(targets_raw) > _MAX_TARGETS:
            raise ValueError(f"plan step {step_id!r} has too many targets")
        targets = tuple(str(item).strip() for item in targets_raw)
        if any(not item or len(item) > _MAX_TARGET_CHARS for item in targets):
            raise ValueError(f"plan step {step_id!r} contains an invalid target")
        verification = str(raw.get("verification", "")).strip()
        if len(verification) > _MAX_VERIFICATION_CHARS:
            raise ValueError(f"plan step {step_id!r} verification is too long")
        status = PlanStepStatus.PENDING
        if allow_status and raw.get("status") is not None:
            try:
                status = PlanStepStatus(str(raw["status"]).strip().lower())
            except ValueError as exc:
                raise ValueError(f"invalid status for plan step {step_id!r}") from exc
        return cls(step_id, description, targets, verification, status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "description": self.description,
            "targets": list(self.targets),
            "verification": self.verification,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    goal: str
    steps: tuple[PlanStep, ...]
    version: int = 1
    previous_version: int | None = None
    revision_reason: str | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        version: int = 1,
        previous_version: int | None = None,
        revision_reason: str | None = None,
        allow_status: bool = False,
    ) -> "ExecutionPlan":
        if not isinstance(payload, dict):
            raise ValueError("plan payload must be an object")
        goal = str(payload.get("goal", "")).strip()
        if not goal:
            raise ValueError("plan goal cannot be empty")
        if len(goal) > _MAX_GOAL_CHARS:
            raise ValueError("plan goal is too long")
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("plan requires at least one step")
        if len(raw_steps) > _MAX_PLAN_STEPS:
            raise ValueError(f"plan may contain at most {_MAX_PLAN_STEPS} steps")
        steps = tuple(PlanStep.from_dict(item, allow_status=allow_status) for item in raw_steps)
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        return cls(
            goal=goal,
            steps=steps,
            version=version,
            previous_version=previous_version,
            revision_reason=revision_reason,
        )

    @property
    def current_step_id(self) -> str | None:
        for step in self.steps:
            if step.status is PlanStepStatus.IN_PROGRESS:
                return step.step_id
        for step in self.steps:
            if step.status is PlanStepStatus.PENDING:
                return step.step_id
        return None

    def with_step_status(self, step_id: str, status: PlanStepStatus) -> "ExecutionPlan":
        found = False
        updated: list[PlanStep] = []
        for step in self.steps:
            if step.step_id == step_id:
                found = True
                if step.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
                    raise ValueError(f"plan step {step_id!r} is already terminal")
                updated.append(replace(step, status=status))
            else:
                updated.append(step)
        if not found:
            raise ValueError(f"unknown plan step id: {step_id!r}")
        return replace(self, steps=tuple(updated))

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "version": self.version,
            "previous_version": self.previous_version,
            "revision_reason": self.revision_reason,
            "current_step": self.current_step_id,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class PlanRevision:
    previous_version: int
    new_version: int
    reason: str


@dataclass(frozen=True)
class PlanControlResult:
    accepted: bool
    message: str
    event_type: EventType
    payload: dict[str, Any]


def decide_planning(task: Task, mode: str | PlanningMode) -> PlanningDecision:
    parsed = PlanningMode.parse(mode)
    if parsed is PlanningMode.OFF:
        return PlanningDecision(parsed, False, "planning_mode_off")
    if parsed is PlanningMode.ALWAYS:
        return PlanningDecision(parsed, True, "planning_mode_always")

    description = " ".join(task.description.lower().split())
    if task.require_tests:
        return PlanningDecision(parsed, True, "auto_require_tests")
    path_hints = set(_PATH_HINT_RE.findall(task.description))
    if len(path_hints) >= 2:
        return PlanningDecision(parsed, True, "auto_multiple_file_hints")
    complex_markers = (" then ", " after ", " across ", " multiple ", "multi-file", "several files")
    if any(marker in f" {description} " for marker in complex_markers):
        return PlanningDecision(parsed, True, "auto_multistep_language")
    return PlanningDecision(parsed, False, "auto_simple_task")


def _step_schema(*, allow_status: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "id": {"type": "string", "description": "Stable short step id, e.g. inspect or edit-parser"},
        "description": {"type": "string", "description": "Concrete step objective"},
        "targets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Expected files, symbols, or repository areas",
        },
        "verification": {"type": "string", "description": "How this step will be verified"},
    }
    if allow_status:
        properties["status"] = {
            "type": "string",
            "enum": [status.value for status in PlanStepStatus],
            "description": "Current explicit status carried into the revised plan",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["id", "description"],
        "additionalProperties": False,
    }


def planning_control_schemas() -> tuple[LLMToolSchema, ...]:
    return (
        LLMToolSchema(
            name=PLAN_CREATE,
            description=(
                "Create the structured execution plan after enough read-only exploration. "
                "This is an internal planning control, not a repository tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "steps": {"type": "array", "items": _step_schema(allow_status=False)},
                },
                "required": ["goal", "steps"],
                "additionalProperties": False,
            },
        ),
        LLMToolSchema(
            name=PLAN_STEP_UPDATE,
            description=(
                "Explicitly update one current plan step. Mark in_progress before working "
                "when useful, then completed or skipped when semantically justified."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            PlanStepStatus.IN_PROGRESS.value,
                            PlanStepStatus.COMPLETED.value,
                            PlanStepStatus.SKIPPED.value,
                        ],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["step_id", "status"],
                "additionalProperties": False,
            },
        ),
        LLMToolSchema(
            name=PLAN_REVISE,
            description=(
                "Replace the current plan when new repository evidence makes it outdated. "
                "Supply the full new current plan and a concise revision reason."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "goal": {"type": "string"},
                    "steps": {"type": "array", "items": _step_schema(allow_status=True)},
                },
                "required": ["reason", "goal", "steps"],
                "additionalProperties": False,
            },
        ),
    )


class PlanningRuntime:
    """Mutable holder for one run's typed plan plus revision lineage."""

    def __init__(self, decision: PlanningDecision) -> None:
        self.decision = decision
        self.current_plan: ExecutionPlan | None = None
        self.revisions: list[PlanRevision] = []

    @property
    def requires_plan(self) -> bool:
        return self.decision.enabled

    def is_control(self, name: str) -> bool:
        return self.decision.enabled and name in PLAN_CONTROL_NAMES

    def schemas(self) -> tuple[LLMToolSchema, ...]:
        return planning_control_schemas() if self.decision.enabled else ()

    def render_context(self) -> str:
        if not self.decision.enabled:
            return ""
        if self.current_plan is None:
            return (
                "[Structured Planning]\n"
                f"Decision: {self.decision.reason}.\n"
                "No execution plan exists yet. Read-only exploration is allowed, but create "
                "a plan with plan_create before any repository-mutating tool call.\n"
                "plan_create requires a non-empty goal and a non-empty steps list; each step "
                "requires a non-empty id and description (for example: inspect, edit, verify).\n"
                "The plan is runtime state; it does not prove task completion."
            )
        plan = self.current_plan
        lines = [
            "[Structured Planning]",
            f"Decision: {self.decision.reason}.",
            f"Plan v{plan.version}: {plan.goal}",
            f"Current step: {plan.current_step_id or 'none'}",
        ]
        for step in plan.steps:
            suffix = ""
            if step.targets:
                suffix += " | targets: " + ", ".join(step.targets)
            if step.verification:
                suffix += " | verify: " + step.verification
            lines.append(f"- [{step.status.value}] {step.step_id}: {step.description}{suffix}")
        lines.append(
            "Use plan_step_update for explicit progress and plan_revise only when new evidence "
            "makes the current plan outdated. Plan status never replaces tests or acceptance."
        )
        return "\n".join(lines)

    def apply_control(self, name: str, params: dict[str, Any]) -> PlanControlResult:
        try:
            if name == PLAN_CREATE:
                return self._create(params)
            if name == PLAN_STEP_UPDATE:
                return self._update_step(params)
            if name == PLAN_REVISE:
                return self._revise(params)
            raise ValueError(f"unknown planning control: {name}")
        except (TypeError, ValueError) as exc:
            hint = ""
            if name == PLAN_CREATE:
                hint = (
                    " Expected plan_create params: non-empty goal; non-empty steps; "
                    "each step must include non-empty id and description."
                )
            return PlanControlResult(
                accepted=False,
                message=f"[PLANNING CONTROL REJECTED] {exc}.{hint}",
                event_type=EventType.PLAN_REJECTED,
                payload={"control": name, "error": str(exc)},
            )

    def _create(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is not None:
            raise ValueError("a plan already exists; use plan_revise")
        plan = ExecutionPlan.from_payload(params)
        self.current_plan = plan
        return PlanControlResult(
            True,
            f"[PLANNING] Created execution plan v{plan.version}.",
            EventType.PLAN_CREATED,
            {"decision_reason": self.decision.reason, "plan": plan.to_dict()},
        )

    def _update_step(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is None:
            raise ValueError("no current plan; create one first")
        step_id = str(params.get("step_id", "")).strip()
        if not step_id:
            raise ValueError("step_id is required")
        try:
            status = PlanStepStatus(str(params.get("status", "")).strip().lower())
        except ValueError as exc:
            raise ValueError("status must be in_progress, completed, or skipped") from exc
        if status is PlanStepStatus.PENDING:
            raise ValueError("plan_step_update cannot reset a step to pending")
        self.current_plan = self.current_plan.with_step_status(step_id, status)
        event_type = (
            EventType.PLAN_STEP_STARTED
            if status is PlanStepStatus.IN_PROGRESS
            else EventType.PLAN_STEP_COMPLETED
        )
        reason = str(params.get("reason", "")).strip()[:300]
        return PlanControlResult(
            True,
            f"[PLANNING] Step {step_id} is now {status.value}.",
            event_type,
            {
                "plan_version": self.current_plan.version,
                "step_id": step_id,
                "step_status": status.value,
                "reason": reason,
            },
        )

    def _revise(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is None:
            raise ValueError("no current plan; use plan_create")
        reason = str(params.get("reason", "")).strip()
        if not reason:
            raise ValueError("plan revision requires a reason")
        if len(reason) > 500:
            raise ValueError("plan revision reason is too long")
        previous = self.current_plan.version
        plan = ExecutionPlan.from_payload(
            params,
            version=previous + 1,
            previous_version=previous,
            revision_reason=reason,
            allow_status=True,
        )
        self.current_plan = plan
        self.revisions.append(PlanRevision(previous, plan.version, reason))
        return PlanControlResult(
            True,
            f"[PLANNING] Revised execution plan v{previous} -> v{plan.version}.",
            EventType.PLAN_REVISED,
            {
                "previous_version": previous,
                "new_version": plan.version,
                "reason": reason,
                "plan": plan.to_dict(),
            },
        )


__all__ = [
    "ExecutionPlan",
    "PlanControlResult",
    "PlanRevision",
    "PlanStep",
    "PlanStepStatus",
    "PlanningDecision",
    "PlanningMode",
    "PlanningRuntime",
    "decide_planning",
    "planning_control_schemas",
]
