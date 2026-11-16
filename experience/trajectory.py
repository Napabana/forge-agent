"""Load immutable Forge Trace/Eval artifacts and normalize them for mining."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from experience.schema import (
    ExperiencePattern,
    NormalizedTrajectory,
    PatternType,
    TrajectoryRef,
)

_MAX_TRACE_BYTES = 16 * 1024 * 1024
_MAX_TRACE_EVENTS = 20_000

_INSPECT_TOOLS = {"file_read", "file_view", "grep", "search", "repo_map", "repo_search"}
_EDIT_TOOLS = {"file_write", "file_edit", "apply_patch"}
_TEST_TOOLS = {"test", "pytest"}
MINING_STRATEGY_VERSION = "recovery_motif_v2"


@dataclass(frozen=True)
class LoadedTrajectory:
    normalized: NormalizedTrajectory
    events: tuple[dict[str, Any], ...]


def _dedupe_consecutive(items: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for item in items:
        if not result or result[-1] != item:
            result.append(item)
    return tuple(result)


def _recovery_motifs(workflow: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Extract bounded typed recovery motifs from a full normalized workflow.

    A motif is anchored by one classified failure and one recovery strategy selected
    before the next classified failure. At most the first semantic action after that
    recovery is retained as local context. Multiple recovery escalations for the same
    failure therefore remain independently mineable. Full workflow provenance stays
    on the source trajectory and is deliberately not part of the grouping key.
    """
    motifs: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for index, token in enumerate(workflow):
        if not token.startswith("FAIL:"):
            continue
        failure = token.split(":", 1)[1]
        for cursor in range(index + 1, len(workflow)):
            current = workflow[cursor]
            if current.startswith("FAIL:"):
                break
            if not current.startswith("RECOVER:"):
                continue
            recovery = current.split(":", 1)[1]
            context: str | None = None
            for next_cursor in range(cursor + 1, len(workflow)):
                next_token = workflow[next_cursor]
                if next_token.startswith(("FAIL:", "RECOVER:")):
                    break
                context = next_token
                break
            motif = (f"failure:{failure}", f"recovery:{recovery}")
            if context is not None:
                motif += (context,)
            if motif not in seen:
                seen.add(motif)
                motifs.append(motif)
    return tuple(motifs)


def _load_trace(path: Path) -> tuple[tuple[dict[str, Any], ...], str]:
    if not path.is_file():
        raise ValueError(f"trace artifact does not exist: {path}")
    size = path.stat().st_size
    if size > _MAX_TRACE_BYTES:
        raise ValueError(f"trace artifact exceeds {_MAX_TRACE_BYTES} bytes")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("trace artifact must be UTF-8 JSONL") from exc
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid trace JSON at line {line_number}: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"trace line {line_number} must be a JSON object")
        events.append(event)
        if len(events) > _MAX_TRACE_EVENTS:
            raise ValueError(f"trace artifact exceeds {_MAX_TRACE_EVENTS} events")
    if not events:
        raise ValueError("trace artifact is empty")
    return tuple(events), digest


def _load_trial_result(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.is_file():
        raise ValueError(f"trial result does not exist: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("trial result must be a JSON object")
    return raw


def _last_event(events: tuple[dict[str, Any], ...], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _tool_operation(tool_name: str, payload: dict[str, Any]) -> str:
    if tool_name in _INSPECT_TOOLS:
        return "INSPECT"
    if tool_name in _EDIT_TOOLS:
        return "EDIT"
    if tool_name in _TEST_TOOLS:
        return "TEST"
    if payload.get("capability_provider") == "mcp" or tool_name.startswith("mcp__"):
        return "EXTERNAL_TOOL"
    if tool_name == "shell":
        return "SHELL"
    return "TOOL"


def load_trajectory(
    trace_path: str | Path,
    *,
    trial_result_path: str | Path | None = None,
) -> LoadedTrajectory:
    trace = Path(trace_path).resolve()
    events, digest = _load_trace(trace)
    trial_path = Path(trial_result_path).resolve() if trial_result_path is not None else None
    trial = _load_trial_result(trial_path)

    task_start = _last_event(events, "task_start")
    terminated = _last_event(events, "run_terminated")
    acceptance = _last_event(events, "acceptance")
    if task_start is None or terminated is None:
        raise ValueError("trajectory requires task_start and run_terminated events")

    task_payload = task_start.get("payload") or {}
    task = task_payload.get("task") or {}
    run_payload = terminated.get("payload") or {}
    acceptance_payload = (acceptance.get("payload") or {}) if acceptance else {}
    run_id = str(task_payload.get("run_id") or run_payload.get("run_id") or trace.stem)
    task_id = str(task_start.get("task_id") or task.get("task_id") or "")
    run_status = str(run_payload.get("status") or "")
    acceptance_status = str(acceptance_payload.get("acceptance_status") or "not_requested")
    termination_reason = run_payload.get("termination_reason")

    suite_id = trial_id = eval_task_id = trial_ref = None
    trial_success: bool | None = None
    if trial is not None:
        suite_id = str(trial.get("suite_id") or "") or None
        trial_id = str(trial.get("trial_id") or "") or None
        eval_task_id = str(trial.get("task_id") or "") or None
        trial_ref = str(trial_path)
        trial_success = bool(trial.get("success", False))
        run_status = str(trial.get("run_status") or run_status)
        acceptance_status = str(trial.get("acceptance_status") or acceptance_status)
        termination_reason = trial.get("termination_reason", termination_reason)

    ref = TrajectoryRef(
        run_id=run_id,
        task_id=task_id,
        trace_ref=str(trace),
        trace_sha256=digest,
        run_status=run_status,
        acceptance_status=acceptance_status,
        termination_reason=str(termination_reason) if termination_reason is not None else None,
        suite_id=suite_id,
        trial_id=trial_id,
        eval_task_id=eval_task_id,
        trial_result_ref=trial_ref,
    )

    if run_status != "success":
        reason = f"run_status={run_status or 'missing'}"
        eligible = False
    elif acceptance_status != "passed":
        reason = f"acceptance_status={acceptance_status or 'missing'}"
        eligible = False
    elif trial_success is False:
        reason = "eval_trial_success=false"
        eligible = False
    else:
        reason = "success_with_acceptance_pass"
        eligible = True

    workflow: list[str] = []
    failure_categories: list[str] = []
    recovery_strategies: list[str] = []
    loaded_skills: list[str] = []
    tools: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") or {}
        if event_type == "plan_created":
            workflow.append("PLAN")
        elif event_type == "plan_revised":
            workflow.append("REPLAN")
        elif event_type == "tool_execution_started":
            tool_name = str(payload.get("tool_name") or "")
            if tool_name:
                tools.append(tool_name)
                workflow.append(_tool_operation(tool_name, payload))
        elif event_type == "failure_classified":
            category = str(payload.get("category") or "")
            if category:
                failure_categories.append(category)
                workflow.append(f"FAIL:{category}")
        elif event_type == "recovery_selected":
            strategy = str(payload.get("strategy") or "")
            if strategy:
                recovery_strategies.append(strategy)
                workflow.append(f"RECOVER:{strategy}")
        elif event_type == "skill_loaded":
            skill = str(payload.get("skill") or "")
            if skill:
                loaded_skills.append(skill)
        elif event_type == "completion_rejected":
            workflow.append("COMPLETION_REJECTED")
        elif event_type == "task_complete":
            workflow.append("FINISH")

    normalized = NormalizedTrajectory(
        ref=ref,
        workflow=_dedupe_consecutive(workflow),
        failure_categories=tuple(failure_categories),
        recovery_strategies=tuple(recovery_strategies),
        loaded_skills=tuple(sorted(set(loaded_skills))),
        tools=tuple(tools),
        eligible=eligible,
        eligibility_reason=reason,
    )
    return LoadedTrajectory(normalized=normalized, events=events)


class ExperienceMiner:
    """Deterministically mine reusable patterns from eligible typed trajectories.

    Successful workflows keep the original exact full-workflow grouping. Recovery
    workflows are mined as bounded local motifs so harmless differences elsewhere
    in a run do not prevent repeated recovery evidence from aggregating.
    """

    def mine(self, trajectories: Iterable[NormalizedTrajectory]) -> tuple[ExperiencePattern, ...]:
        groups: dict[tuple[str, tuple[str, ...]], list[NormalizedTrajectory]] = {}
        for trajectory in trajectories:
            if not trajectory.eligible or not trajectory.workflow:
                continue
            if trajectory.has_recovery:
                for signature in _recovery_motifs(trajectory.workflow):
                    groups.setdefault(
                        (PatternType.RECOVERY_WORKFLOW.value, signature),
                        [],
                    ).append(trajectory)
            else:
                groups.setdefault(
                    (PatternType.SUCCESSFUL_WORKFLOW.value, trajectory.workflow),
                    [],
                ).append(trajectory)

        patterns: list[ExperiencePattern] = []
        for (pattern_type_value, signature), members in groups.items():
            ordered = sorted(
                members,
                key=lambda item: (
                    item.ref.run_id,
                    item.ref.task_id,
                    item.ref.trace_sha256,
                    item.ref.trace_ref,
                ),
            )
            pattern_type = PatternType(pattern_type_value)
            if pattern_type is PatternType.RECOVERY_WORKFLOW:
                failure_categories = tuple(
                    token.split(":", 1)[1]
                    for token in signature
                    if token.startswith("failure:")
                )
                recovery_strategies = tuple(
                    token.split(":", 1)[1]
                    for token in signature
                    if token.startswith("recovery:")
                )
            else:
                failure_categories = ()
                recovery_strategies = ()
            patterns.append(ExperiencePattern.build(
                pattern_type=pattern_type,
                signature=signature,
                source_trajectories=tuple(member.ref for member in ordered),
                failure_categories=failure_categories,
                recovery_strategies=recovery_strategies,
            ))
        return tuple(sorted(patterns, key=lambda item: item.pattern_id))
