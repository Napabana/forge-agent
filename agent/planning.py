"""Structured Planning runtime state for P2-1 / Planning v2.

The LLM owns semantic planning decisions. Runtime owns stable step identity, version
lineage, legal state transitions, and the currently valid planning control surface.
"""
from __future__ import annotations

import re
import unicodedata
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
_MAX_STEP_ID_CHARS = 64
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


def _normalized_description(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _step_slug(description: str, index: int) -> str:
    normalized = unicodedata.normalize("NFKD", description)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    if not slug:
        slug = f"step-{index}"
    return slug[:_MAX_STEP_ID_CHARS].rstrip("-") or f"step-{index}"


def _allocate_step_id(description: str, index: int, used: set[str]) -> str:
    base = _step_slug(description, index)
    candidate = base
    suffix = 2
    while candidate in used:
        suffix_text = f"-{suffix}"
        stem = base[: _MAX_STEP_ID_CHARS - len(suffix_text)].rstrip("-")
        candidate = f"{stem or 'step'}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    description: str
    targets: tuple[str, ...] = ()
    verification: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        allow_status: bool,
        step_id: str | None = None,
    ) -> "PlanStep":
        if not isinstance(raw, dict):
            raise ValueError("plan step must be an object")
        resolved_id = str(step_id if step_id is not None else raw.get("id", "")).strip()
        if not _STEP_ID_RE.fullmatch(resolved_id):
            raise ValueError(f"invalid plan step id: {resolved_id!r}")
        description = str(raw.get("description") or "").strip()
        if not description:
            raise ValueError(f"plan step {resolved_id!r} requires a description")
        if len(description) > _MAX_DESCRIPTION_CHARS:
            raise ValueError(f"plan step {resolved_id!r} description is too long")
        targets_raw = raw.get("targets", ())
        if targets_raw is None:
            targets_raw = ()
        if not isinstance(targets_raw, (list, tuple)):
            raise ValueError(f"plan step {resolved_id!r} targets must be an array")
        if len(targets_raw) > _MAX_TARGETS:
            raise ValueError(f"plan step {resolved_id!r} has too many targets")
        targets = tuple(str(item).strip() for item in targets_raw)
        if any(not item or len(item) > _MAX_TARGET_CHARS for item in targets):
            raise ValueError(f"plan step {resolved_id!r} contains an invalid target")
        verification = str(raw.get("verification") or "").strip()
        if len(verification) > _MAX_VERIFICATION_CHARS:
            raise ValueError(f"plan step {resolved_id!r} verification is too long")
        status = PlanStepStatus.PENDING
        if allow_status and raw.get("status") is not None:
            try:
                status = PlanStepStatus(str(raw["status"]).strip().lower())
            except ValueError as exc:
                raise ValueError(f"invalid status for plan step {resolved_id!r}") from exc
        return cls(resolved_id, description, targets, verification, status)

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
        runtime_owned_ids: bool = False,
        previous_plan: "ExecutionPlan | None" = None,
    ) -> "ExecutionPlan":
        if not isinstance(payload, dict):
            raise ValueError("plan payload must be an object")
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise ValueError("plan goal cannot be empty")
        if len(goal) > _MAX_GOAL_CHARS:
            raise ValueError("plan goal is too long")
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("plan requires at least one step")
        if len(raw_steps) > _MAX_PLAN_STEPS:
            raise ValueError(f"plan may contain at most {_MAX_PLAN_STEPS} steps")

        if not runtime_owned_ids:
            steps = tuple(
                PlanStep.from_dict(item, allow_status=allow_status) for item in raw_steps
            )
            ids = [step.step_id for step in steps]
            if len(ids) != len(set(ids)):
                raise ValueError("plan step ids must be unique")
        else:
            previous_by_id = {
                step.step_id: step for step in previous_plan.steps
            } if previous_plan is not None else {}
            previous_by_description: dict[str, list[PlanStep]] = {}
            if previous_plan is not None:
                for step in previous_plan.steps:
                    previous_by_description.setdefault(
                        _normalized_description(step.description), []
                    ).append(step)

            used: set[str] = set()
            consumed_previous: set[str] = set()
            built: list[PlanStep] = []
            for index, raw in enumerate(raw_steps, start=1):
                if not isinstance(raw, dict):
                    raise ValueError("plan step must be an object")
                explicit_id = str(raw.get("id") or "").strip()
                previous_step = previous_by_id.get(explicit_id)
                if previous_step is not None and previous_step.step_id in consumed_previous:
                    previous_step = None
                if previous_step is None:
                    for candidate in previous_by_description.get(
                        _normalized_description(raw.get("description")), []
                    ):
                        if candidate.step_id not in consumed_previous:
                            previous_step = candidate
                            break
                if previous_step is not None:
                    step_id = previous_step.step_id
                    used.add(step_id)
                    consumed_previous.add(step_id)
                else:
                    description = str(raw.get("description") or "").strip()
                    step_id = _allocate_step_id(description, index, used)
                step = PlanStep.from_dict(raw, allow_status=False, step_id=step_id)
                if previous_step is not None:
                    step = replace(step, status=previous_step.status)
                built.append(step)
            steps = tuple(built)

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

    def get_step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise ValueError(f"unknown plan step id: {step_id!r}")

    def with_step_status(self, step_id: str, status: PlanStepStatus) -> "ExecutionPlan":
        current = self.get_step(step_id)
        if current.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
            raise ValueError(f"plan step {step_id!r} is already terminal")
        return replace(
            self,
            steps=tuple(
                replace(step, status=status) if step.step_id == step_id else step
                for step in self.steps
            ),
        )

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


def _step_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Concrete step objective"},
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expected files, symbols, or repository areas",
            },
            "verification": {"type": "string", "description": "How this step will be verified"},
        },
        "required": ["description"],
        "additionalProperties": False,
    }


def planning_control_schemas(
    current_plan: ExecutionPlan | None = None,
) -> tuple[LLMToolSchema, ...]:
    if current_plan is None:
        return (
            LLMToolSchema(
                name=PLAN_CREATE,
                description=(
                    "Create the semantic execution plan after enough read-only exploration. "
                    "Runtime assigns stable step ids and owns plan bookkeeping."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "steps": {"type": "array", "items": _step_schema()},
                    },
                    "required": ["goal", "steps"],
                    "additionalProperties": False,
                },
            ),
        )

    valid_ids = [step.step_id for step in current_plan.steps]
    return (
        LLMToolSchema(
            name=PLAN_STEP_UPDATE,
            description=(
                "Update one runtime-owned plan step. Repeating the same status is an "
                "idempotent no-op; rollback from a terminal state is rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "step_id": {"type": "string", "enum": valid_ids},
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
                "Revise semantic plan steps when new repository evidence changes the approach. "
                "Runtime carries version, identity, and compatible prior terminal progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "goal": {"type": "string"},
                    "steps": {"type": "array", "items": _step_schema()},
                },
                "required": ["reason", "steps"],
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
        self._legacy_step_aliases: dict[str, str] = {}

    @property
    def requires_plan(self) -> bool:
        return self.decision.enabled

    def is_control(self, name: str) -> bool:
        return self.decision.enabled and name in PLAN_CONTROL_NAMES

    def schemas(self) -> tuple[LLMToolSchema, ...]:
        if not self.decision.enabled:
            return ()
        return planning_control_schemas(self.current_plan)

    def render_context(self) -> str:
        if not self.decision.enabled:
            return ""
        if self.current_plan is None:
            return (
                "[Structured Planning]\n"
                f"Decision: {self.decision.reason}.\n"
                "No execution plan exists yet. Read-only exploration is allowed, but create "
                "a plan with plan_create before any repository-mutating tool call.\n"
                "plan_create requires a non-empty goal and non-empty semantic steps; each "
                "step needs a description while Runtime assigns stable step ids.\n"
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
            "Use the runtime-owned step ids shown above for plan_step_update. "
            "Use plan_revise only when new evidence makes the semantic plan outdated; "
            "Runtime owns version/status carry-forward. Plan status never replaces tests or acceptance."
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
                    "each step must include a non-empty description. Runtime assigns step ids."
                )
            elif name == PLAN_STEP_UPDATE:
                candidates = (
                    [step.step_id for step in self.current_plan.steps]
                    if self.current_plan is not None
                    else []
                )
                available = ", ".join(candidates) or "none"
                hint = (
                    " Expected plan_step_update params: step_id must name an existing runtime "
                    "step and status must be in_progress, completed, or skipped. "
                    f"Current step ids: {available}."
                )
            return PlanControlResult(
                accepted=False,
                message=f"[PLANNING CONTROL REJECTED] {exc}.{hint}",
                event_type=EventType.PLAN_REJECTED,
                payload={"control": name, "error": str(exc), "state_changed": False},
            )

    def _record_legacy_aliases(
        self,
        raw_steps: list[Any],
        plan: ExecutionPlan,
    ) -> None:
        valid_ids = {step.step_id for step in plan.steps}
        self._legacy_step_aliases = {
            alias: target
            for alias, target in self._legacy_step_aliases.items()
            if target in valid_ids
        }
        for raw, step in zip(raw_steps, plan.steps):
            if not isinstance(raw, dict):
                continue
            alias = str(raw.get("id") or "").strip()
            if _STEP_ID_RE.fullmatch(alias):
                self._legacy_step_aliases.setdefault(alias, step.step_id)

    def _resolve_step_id(self, raw_step_id: str) -> str:
        if self.current_plan is None:
            return raw_step_id
        if any(step.step_id == raw_step_id for step in self.current_plan.steps):
            return raw_step_id
        return self._legacy_step_aliases.get(raw_step_id, raw_step_id)

    def _create(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is not None:
            raise ValueError("a plan already exists; use plan_revise")
        plan = ExecutionPlan.from_payload(params, runtime_owned_ids=True)
        self.current_plan = plan
        self._record_legacy_aliases(params.get("steps", []), plan)
        return PlanControlResult(
            True,
            f"[PLANNING] Created execution plan v{plan.version}.",
            EventType.PLAN_CREATED,
            {
                "decision_reason": self.decision.reason,
                "plan": plan.to_dict(),
                "state_changed": True,
            },
        )

    def _update_step(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is None:
            raise ValueError("no current plan; create one first")
        raw_step_id = str(params.get("step_id") or "").strip()
        if not raw_step_id:
            raise ValueError("step_id is required")
        step_id = self._resolve_step_id(raw_step_id)
        try:
            status = PlanStepStatus(str(params.get("status") or "").strip().lower())
        except ValueError as exc:
            raise ValueError("status must be in_progress, completed, or skipped") from exc
        if status is PlanStepStatus.PENDING:
            raise ValueError("plan_step_update cannot reset a step to pending")
        current = self.current_plan.get_step(step_id)
        reason = str(params.get("reason") or "").strip()[:300]

        if current.status is status:
            event_type = (
                EventType.PLAN_STEP_STARTED
                if status is PlanStepStatus.IN_PROGRESS
                else EventType.PLAN_STEP_COMPLETED
            )
            return PlanControlResult(
                True,
                f"[PLANNING] Step {step_id} is already {status.value}; no state change.",
                event_type,
                {
                    "plan_version": self.current_plan.version,
                    "step_id": step_id,
                    "step_status": status.value,
                    "reason": reason,
                    "idempotent": True,
                    "state_changed": False,
                },
            )
        if current.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
            raise ValueError(
                f"plan step {step_id!r} is terminal at {current.status.value}; rollback is not allowed"
            )

        self.current_plan = self.current_plan.with_step_status(step_id, status)
        event_type = (
            EventType.PLAN_STEP_STARTED
            if status is PlanStepStatus.IN_PROGRESS
            else EventType.PLAN_STEP_COMPLETED
        )
        return PlanControlResult(
            True,
            f"[PLANNING] Step {step_id} is now {status.value}.",
            event_type,
            {
                "plan_version": self.current_plan.version,
                "step_id": step_id,
                "step_status": status.value,
                "reason": reason,
                "idempotent": False,
                "state_changed": True,
            },
        )

    def _revise(self, params: dict[str, Any]) -> PlanControlResult:
        if self.current_plan is None:
            raise ValueError("no current plan; use plan_create")
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise ValueError("plan revision requires a reason")
        if len(reason) > 500:
            raise ValueError("plan revision reason is too long")
        raw_steps = params.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("plan requires at least one step")
        goal = str(params.get("goal") or self.current_plan.goal).strip()
        previous_plan = self.current_plan
        previous = previous_plan.version
        plan = ExecutionPlan.from_payload(
            {"goal": goal, "steps": raw_steps},
            version=previous + 1,
            previous_version=previous,
            revision_reason=reason,
            runtime_owned_ids=True,
            previous_plan=previous_plan,
        )
        self.current_plan = plan
        self._record_legacy_aliases(raw_steps, plan)
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
                "state_changed": True,
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
