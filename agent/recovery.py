"""Structured failure classification and bounded recovery policy for P2-2.

This module does not execute tools, call providers, or own a second agent loop.
It only converts runtime failure evidence into an inspectable RecoveryDecision.
The existing Agent loop remains responsible for carrying out the next action.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RecoveryMode(str, Enum):
    OFF = "off"
    STRUCTURED = "structured"

    @classmethod
    def parse(cls, value: str | "RecoveryMode") -> "RecoveryMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError("recovery_mode must be one of: off, structured") from exc


class FailureCategory(str, Enum):
    TEST_FAILURE = "test_failure"
    TOOL_FAILURE = "tool_failure"
    PERMISSION_DENIED = "permission_denied"
    LOOP = "loop"
    NO_PROGRESS = "no_progress"
    COMPLETION_REJECTED = "completion_rejected"
    INFRASTRUCTURE = "infrastructure"


class FailureSource(str, Enum):
    TOOL = "tool"
    TEST = "test"
    LOOP_DETECTOR = "loop_detector"
    PROGRESS_GUARD = "progress_guard"
    COMPLETION_GUARD = "completion_guard"


class RecoveryStrategy(str, Enum):
    RETRY = "retry"
    INSPECT = "inspect"
    RERUN_TEST = "rerun_test"
    CHANGE_APPROACH = "change_approach"
    REPLAN = "replan"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class FailureContext:
    category: FailureCategory
    source: FailureSource
    step: int
    evidence: str
    tool_name: str | None = None
    error_type: str | None = None
    completion_code: str | None = None
    recent_actions: tuple[str, ...] = ()
    repository_changed: bool = False
    test_state: str | None = None
    plan_version: int | None = None
    plan_step_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "source": self.source.value,
            "evidence": self.evidence[:1200],
            "tool_name": self.tool_name,
            "error_type": self.error_type,
            "completion_code": self.completion_code,
            "recent_actions": list(self.recent_actions[-4:]),
            "repository_changed": self.repository_changed,
            "test_state": self.test_state,
            "plan_state": {
                "version": self.plan_version,
                "current_step_id": self.plan_step_id,
            },
        }


@dataclass(frozen=True)
class RecoveryDecision:
    strategy: RecoveryStrategy
    reason: str
    attempt: int
    max_attempts: int
    target_plan_step: str | None = None
    requires_plan_revision: bool = False
    terminal: bool = False
    budget_exhausted: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "reason": self.reason,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "target_plan_step": self.target_plan_step,
            "requires_plan_revision": self.requires_plan_revision,
            "terminal": self.terminal,
            "budget_exhausted": self.budget_exhausted,
        }


class RecoveryPolicy:
    """Deterministic policy over already-observed runtime failures."""

    def __init__(
        self,
        mode: str | RecoveryMode = RecoveryMode.OFF,
        *,
        max_attempts: int = 4,
    ) -> None:
        self.mode = RecoveryMode.parse(mode)
        if max_attempts < 1:
            raise ValueError("recovery max_attempts must be >= 1")
        self.max_attempts = int(max_attempts)
        self._attempts = 0
        self._category_counts: dict[FailureCategory, int] = {}

    @property
    def enabled(self) -> bool:
        return self.mode is RecoveryMode.STRUCTURED

    @property
    def attempts_used(self) -> int:
        return self._attempts

    def decide(self, context: FailureContext) -> RecoveryDecision | None:
        if not self.enabled:
            return None

        # Infrastructure is classified for completeness but P2-2 does not
        # reinterpret the existing fatal infrastructure contract as recovery.
        if context.category is FailureCategory.INFRASTRUCTURE:
            return RecoveryDecision(
                RecoveryStrategy.GIVE_UP,
                "Infrastructure failures remain governed by the existing fatal contract.",
                attempt=self._attempts,
                max_attempts=self.max_attempts,
                terminal=True,
            )

        if self._attempts >= self.max_attempts:
            return RecoveryDecision(
                RecoveryStrategy.GIVE_UP,
                "Structured recovery budget exhausted; stop instead of cycling indefinitely.",
                attempt=self._attempts,
                max_attempts=self.max_attempts,
                target_plan_step=context.plan_step_id,
                terminal=True,
                budget_exhausted=True,
            )

        self._attempts += 1
        occurrence = self._category_counts.get(context.category, 0) + 1
        self._category_counts[context.category] = occurrence
        strategy, reason = self._choose_strategy(context, occurrence)
        requires_revision = (
            strategy is RecoveryStrategy.REPLAN and context.plan_version is not None
        )
        return RecoveryDecision(
            strategy=strategy,
            reason=reason,
            attempt=self._attempts,
            max_attempts=self.max_attempts,
            target_plan_step=context.plan_step_id,
            requires_plan_revision=requires_revision,
        )

    def _choose_strategy(
        self,
        context: FailureContext,
        occurrence: int,
    ) -> tuple[RecoveryStrategy, str]:
        has_plan = context.plan_version is not None

        if context.category is FailureCategory.TEST_FAILURE:
            if occurrence == 1:
                return (
                    RecoveryStrategy.INSPECT,
                    "Inspect the failing test evidence and identify the root cause before editing again.",
                )
            if has_plan:
                return (
                    RecoveryStrategy.REPLAN,
                    "Repeated test failure invalidates the current execution approach; revise the plan.",
                )
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "Repeated test failure requires a materially different diagnosis or implementation approach.",
            )

        if context.category is FailureCategory.PERMISSION_DENIED:
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "The denied capability must not be retried unchanged; choose an allowed approach.",
            )

        if context.category is FailureCategory.LOOP:
            if has_plan:
                return (
                    RecoveryStrategy.REPLAN,
                    "A no-progress action cycle means the current plan should be revised before further mutation.",
                )
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "Break the detected cycle with a materially different tool, target, or hypothesis.",
            )

        if context.category is FailureCategory.NO_PROGRESS:
            if has_plan:
                return (
                    RecoveryStrategy.REPLAN,
                    "Extended exploration without repository progress requires revising the current plan.",
                )
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "Stop broad exploration and choose a concrete next action that can create progress.",
            )

        if context.category is FailureCategory.COMPLETION_REJECTED:
            code = context.completion_code or ""
            if code in {"REQUIRED_TEST_MISSING", "FINAL_STATE_UNVERIFIED"}:
                return (
                    RecoveryStrategy.RERUN_TEST,
                    "Completion requires fresh verification of the current repository state.",
                )
            if code == "LATEST_TEST_FAILED":
                if has_plan:
                    return (
                        RecoveryStrategy.REPLAN,
                        "Completion was rejected because verification still fails; revise the plan around the failure.",
                    )
                return (
                    RecoveryStrategy.INSPECT,
                    "Inspect the latest failing verification before attempting completion again.",
                )
            if code == "REPOSITORY_UNCHANGED":
                if has_plan:
                    return (
                        RecoveryStrategy.REPLAN,
                        "The required repository change did not occur; revise the execution plan.",
                    )
                return (
                    RecoveryStrategy.CHANGE_APPROACH,
                    "The task requires a real repository change; choose an action that can produce one.",
                )
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "Resolve the unmet completion requirement before attempting to finish again.",
            )

        # Generic recoverable tool errors.
        error_type = context.error_type or ""
        if error_type == "timeout":
            if occurrence == 1:
                return (
                    RecoveryStrategy.RETRY,
                    "The tool timed out; retry once only if the operation is still appropriate, otherwise narrow it.",
                )
            if has_plan:
                return (
                    RecoveryStrategy.REPLAN,
                    "Repeated tool timeout means the current approach is too brittle or expensive; revise the plan.",
                )
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "Repeated timeout requires a narrower or different tool strategy.",
            )
        if error_type in {
            "unknown_tool",
            "invalid_arguments",
            "hook_blocked",
            "hook_failed",
        }:
            return (
                RecoveryStrategy.CHANGE_APPROACH,
                "The attempted tool path is unavailable or invalid; choose a valid alternative instead of repeating it.",
            )
        if occurrence == 1:
            return (
                RecoveryStrategy.INSPECT,
                "Inspect the tool error and surrounding repository evidence before choosing the next action.",
            )
        if has_plan:
            return (
                RecoveryStrategy.REPLAN,
                "Repeated tool failure invalidates the current execution approach; revise the plan.",
            )
        return (
            RecoveryStrategy.CHANGE_APPROACH,
            "Repeated tool failure requires a materially different approach.",
        )


class RecoveryRuntime:
    """Per-run recovery state; owns budget and a replan gate, not plan contents."""

    def __init__(
        self,
        mode: str | RecoveryMode = RecoveryMode.OFF,
        *,
        max_attempts: int = 4,
    ) -> None:
        self.policy = RecoveryPolicy(mode, max_attempts=max_attempts)
        self.pending_replan_from_version: int | None = None
        self.last_decision: RecoveryDecision | None = None

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    def select(self, context: FailureContext) -> RecoveryDecision | None:
        decision = self.policy.decide(context)
        self.last_decision = decision
        if (
            decision is not None
            and decision.requires_plan_revision
            and context.plan_version is not None
        ):
            self.pending_replan_from_version = context.plan_version
        return decision

    def requires_replan(self, current_plan_version: int | None) -> bool:
        if self.pending_replan_from_version is None:
            return False
        return (
            current_plan_version is None
            or current_plan_version <= self.pending_replan_from_version
        )

    def observe_plan_revision(self, new_version: int) -> bool:
        pending = self.pending_replan_from_version
        if pending is None or new_version <= pending:
            return False
        self.pending_replan_from_version = None
        return True

    def replan_gate_message(self) -> str:
        pending = self.pending_replan_from_version
        return (
            "[RECOVERY REPLAN REQUIRED] Structured recovery requires plan_revise "
            f"to advance beyond plan v{pending} before further repository mutation. "
            "FINISH remains available and will still be checked by Completion Guard."
        )

    def render_context(self) -> str:
        """Keep an unresolved replan gate visible across history trimming/compaction."""
        if not self.enabled or self.pending_replan_from_version is None:
            return ""
        return (
            "[Structured Recovery State]\n"
            f"Recovery requires a plan revision newer than v{self.pending_replan_from_version}.\n"
            "Read-only diagnosis and FINISH are allowed, but further repository mutation "
            "remains blocked until plan_revise produces a newer plan version. "
            "FINISH still must satisfy Completion Guard."
        )


def classify_tool_failure(
    *,
    tool_name: str,
    error_type: str | None,
    test_tool_names: tuple[str, ...] = ("test", "pytest"),
) -> FailureCategory:
    if error_type == "infrastructure":
        return FailureCategory.INFRASTRUCTURE
    if error_type == "permission_denied":
        return FailureCategory.PERMISSION_DENIED
    if tool_name in test_tool_names:
        return FailureCategory.TEST_FAILURE
    return FailureCategory.TOOL_FAILURE
