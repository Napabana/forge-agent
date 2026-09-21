"""Deterministic graders and Trace/RunResult metric extraction."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent.task import RunResult
from evals.coding_agent.schema import GraderResult, GraderSpec, TrialMetrics

_TEST_TOOLS = {"test", "pytest"}
_FILE_READ_TOOLS = {"file_read", "file_view"}
_INSPECT_TOOLS = {"file_read", "file_view", "search_text", "find_files", "find_symbol", "grep", "search", "repo_map", "repo_search"}
_EDIT_TOOLS = {"file_write", "file_edit", "apply_patch"}


def _semantic_operation(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") or {}
    if event_type == "tool_execution_started":
        tool_name = str(payload.get("tool_name") or "")
        if tool_name in _INSPECT_TOOLS:
            return "INSPECT"
        if tool_name in _EDIT_TOOLS:
            return "EDIT"
        if tool_name in _TEST_TOOLS:
            return "TEST"
        if tool_name == "shell":
            return "SHELL"
        if payload.get("capability_provider") == "mcp" or tool_name.startswith("mcp__"):
            return "EXTERNAL_TOOL"
        return "TOOL" if tool_name else None
    if event_type == "plan_created":
        return "PLAN"
    if event_type == "plan_revised":
        return "REPLAN"
    if event_type == "completion_rejected":
        return "COMPLETION_REJECTED"
    return None


def _grade_recovery_motif(spec: GraderSpec, context: GraderContext) -> GraderResult:
    events = load_trace_events(context.run_result.trace_path if context.run_result else None)
    failure_category = str(spec.params.get("failure_category") or "")
    recovery_strategy = str(spec.params.get("recovery_strategy") or "")
    skill_name = str(spec.params.get("skill_name") or "")
    next_operation = str(spec.params.get("next_operation") or "")

    matched: dict[str, int | str | None] = {
        "failure_index": None,
        "recovery_index": None,
        "skill_index": None,
        "next_operation": None,
    }
    for failure_index, event in enumerate(events):
        payload = event.get("payload") or {}
        if (
            event.get("event_type") != "failure_classified"
            or str(payload.get("category") or "") != failure_category
        ):
            continue
        for recovery_index in range(failure_index + 1, len(events)):
            recovery_event = events[recovery_index]
            recovery_payload = recovery_event.get("payload") or {}
            if recovery_event.get("event_type") == "failure_classified":
                break
            if (
                recovery_event.get("event_type") != "recovery_selected"
                or str(recovery_payload.get("strategy") or "") != recovery_strategy
            ):
                continue
            for skill_index in range(recovery_index + 1, len(events)):
                skill_event = events[skill_index]
                skill_payload = skill_event.get("payload") or {}
                if (
                    skill_event.get("event_type") == "skill_loaded"
                    and str(skill_payload.get("skill") or "") == skill_name
                ):
                    observed_operation = None
                    for operation_index in range(skill_index + 1, len(events)):
                        observed_operation = _semantic_operation(events[operation_index])
                        if observed_operation is not None:
                            break
                    matched = {
                        "failure_index": failure_index,
                        "recovery_index": recovery_index,
                        "skill_index": skill_index,
                        "next_operation": observed_operation,
                    }
                    passed = observed_operation == next_operation
                    detail = (
                        f"failure={failure_category}, recovery={recovery_strategy}, "
                        f"skill={skill_name}, next_operation={observed_operation}, "
                        f"expected={next_operation}"
                    )
                    return GraderResult(
                        spec.grader_id,
                        spec.kind,
                        passed,
                        spec.required,
                        detail,
                        matched,
                    )
            break
    detail = (
        f"required ordered motif not observed: failure={failure_category}, "
        f"recovery={recovery_strategy}, skill={skill_name}, next={next_operation}"
    )
    return GraderResult(
        spec.grader_id,
        spec.kind,
        False,
        spec.required,
        detail,
        matched,
    )


@dataclass(frozen=True)
class GraderContext:
    repo: Path
    run_result: RunResult | None = None
    metrics: TrialMetrics | None = None


def _run_command(repo: Path, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    resolved = [sys.executable if arg == "{python}" else arg for arg in command]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        resolved,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def grade(spec: GraderSpec, context: GraderContext) -> GraderResult:
    """Evaluate one deterministic grader. Graders never mutate Agent History."""
    if spec.kind == "command":
        timeout = float(spec.params.get("timeout_seconds", 10))
        expected = int(spec.params.get("expected_returncode", 0))
        try:
            completed = _run_command(context.repo, list(spec.params["command"]), timeout)
            passed = completed.returncode == expected
            detail = f"returncode={completed.returncode}, expected={expected}"
            evidence = {
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            passed, detail, evidence = False, f"command grader failed: {type(exc).__name__}: {exc}", {}
        return GraderResult(spec.grader_id, spec.kind, passed, spec.required, detail, evidence)

    if spec.kind == "file":
        target = (context.repo / str(spec.params["path"])).resolve()
        try:
            target.relative_to(context.repo.resolve())
        except ValueError:
            return GraderResult(spec.grader_id, spec.kind, False, spec.required, "file path escaped repository")
        exists = target.is_file()
        expected_exists = bool(spec.params.get("exists", True))
        passed = exists == expected_exists
        detail_parts = [f"exists={exists}, expected={expected_exists}"]
        content = ""
        if exists:
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return GraderResult(spec.grader_id, spec.kind, False, spec.required, f"cannot read file: {exc}")
            if "contains" in spec.params:
                needle = str(spec.params["contains"])
                matched = needle in content
                passed = passed and matched
                detail_parts.append(f"contains={matched}")
            if "not_contains" in spec.params:
                needle = str(spec.params["not_contains"])
                matched = needle not in content
                passed = passed and matched
                detail_parts.append(f"not_contains={matched}")
            if "equals" in spec.params:
                matched = content == str(spec.params["equals"])
                passed = passed and matched
                detail_parts.append(f"equals={matched}")
        return GraderResult(spec.grader_id, spec.kind, passed, spec.required, ", ".join(detail_parts))

    if spec.kind == "repository_state":
        completed = subprocess.run(
            ["git", "status", "--porcelain"], cwd=context.repo, capture_output=True,
            text=True, check=False,
        )
        if completed.returncode != 0:
            return GraderResult(
                spec.grader_id, spec.kind, False, spec.required,
                f"git status failed: {completed.stderr.strip()}",
            )
        changed = bool(completed.stdout.strip())
        expected = bool(spec.params["changed"])
        return GraderResult(
            spec.grader_id, spec.kind, changed == expected, spec.required,
            f"changed={changed}, expected={expected}",
            {"status_porcelain": completed.stdout[-4000:]},
        )

    if spec.kind == "recovery_motif":
        return _grade_recovery_motif(spec, context)

    if spec.kind == "skill_selection":
        trace_path = context.run_result.trace_path if context.run_result else None
        selected = {
            str((event.get("payload") or {}).get("skill") or "")
            for event in load_trace_events(trace_path)
            if event.get("event_type") == "skill_loaded"
        }
        selected.discard("")
        expected_any = {str(value) for value in spec.params.get("expected_any", [])}
        forbidden = {str(value) for value in spec.params.get("forbidden", [])}
        expected_ok = not expected_any or bool(selected & expected_any)
        forbidden_ok = not bool(selected & forbidden)
        passed = expected_ok and forbidden_ok
        detail = (
            f"selected={sorted(selected)}, expected_any={sorted(expected_any)}, "
            f"forbidden={sorted(forbidden)}"
        )
        return GraderResult(
            spec.grader_id,
            spec.kind,
            passed,
            spec.required,
            detail,
            {"selected_skills": sorted(selected)},
        )

    metrics = context.metrics or TrialMetrics()
    result = context.run_result
    checks: list[tuple[str, bool]] = []
    if "run_status" in spec.params:
        actual = result.status.value if result is not None else None
        checks.append((f"run_status={actual}", actual == str(spec.params["run_status"])))
    if "termination_reason" in spec.params:
        actual = result.termination_reason if result is not None else None
        checks.append((f"termination_reason={actual}", actual == spec.params["termination_reason"]))
    if "required_tool" in spec.params:
        required = str(spec.params["required_tool"])
        tool_counts = _tool_counts(result.trace_path if result else None)
        checks.append((f"required_tool={required}:{tool_counts.get(required, 0)}", tool_counts.get(required, 0) > 0))
    integer_fields = {
        "min_test_attempts": metrics.test_attempt_count,
        "min_tool_calls": metrics.tool_call_count,
        "min_completion_rejections": metrics.completion_rejection_count,
        "min_reflections": metrics.reflection_count,
    }
    for field, actual in integer_fields.items():
        if field in spec.params:
            expected = int(spec.params[field])
            checks.append((f"{field}={actual}>={expected}", actual >= expected))
    passed = all(ok for _, ok in checks)
    detail = "; ".join(label for label, _ in checks) or "no checks"
    return GraderResult(spec.grader_id, spec.kind, passed, spec.required, detail)


def grade_many(specs: Iterable[GraderSpec], context: GraderContext) -> tuple[GraderResult, ...]:
    return tuple(grade(spec, context) for spec in specs)


def required_graders_passed(results: Iterable[GraderResult]) -> bool:
    return all(result.passed for result in results if result.required)


def load_trace_events(trace_path: str | None) -> list[dict[str, Any]]:
    if not trace_path:
        return []
    path = Path(trace_path)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _tool_counts(trace_path: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in load_trace_events(trace_path):
        if event.get("event_type") != "tool_execution_started":
            continue
        tool = str((event.get("payload") or {}).get("tool_name") or "")
        if tool:
            counts[tool] = counts.get(tool, 0) + 1
    return counts


def extract_metrics(run_result: RunResult, *, wall_time_seconds: float) -> TrialMetrics:
    events = load_trace_events(run_result.trace_path)
    tool_call_count = 0
    test_attempt_count = 0
    completion_rejection_count = 0
    reflection_count = 0
    file_read_count = 0
    shell_call_count = 0
    plan_created_count = 0
    plan_revision_count = 0
    plan_step_completed_count = 0
    planning_skipped = False
    failure_classified_count = 0
    recovery_selected_count = 0
    recovery_replan_count = 0
    recovery_exhausted_count = 0
    skill_discovered_count = 0
    skill_selected_count = 0
    skill_loaded_count = 0
    skill_reference_loaded_count = 0
    mcp_tool_discovered_count = 0
    mcp_tool_call_count = 0
    mcp_tool_failure_count = 0
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type == "tool_execution_started":
            tool_call_count += 1
            tool_name = str(payload.get("tool_name") or "")
            test_attempt_count += int(tool_name in _TEST_TOOLS)
            file_read_count += int(tool_name in _FILE_READ_TOOLS)
            shell_call_count += int(tool_name == "shell")
            if payload.get("capability_provider") == "mcp" or tool_name.startswith("mcp__"):
                mcp_tool_call_count += 1
        elif event_type == "tool_execution_failed":
            tool_name = str(payload.get("tool_name") or "")
            if payload.get("capability_provider") == "mcp" or tool_name.startswith("mcp__"):
                mcp_tool_failure_count += 1
        elif event_type == "mcp_tool_discovered":
            mcp_tool_discovered_count += 1
        elif event_type == "completion_rejected":
            completion_rejection_count += 1
        elif event_type == "reflection":
            reflection_count += 1
        elif event_type == "plan_created":
            plan_created_count += 1
        elif event_type == "plan_revised":
            plan_revision_count += 1
        elif (
            event_type == "plan_step_completed"
            and payload.get("step_status") == "completed"
            and payload.get("state_changed", True)
        ):
            plan_step_completed_count += 1
        elif event_type == "planning_skipped":
            planning_skipped = True
        elif event_type == "failure_classified":
            failure_classified_count += 1
        elif event_type == "recovery_selected":
            recovery_selected_count += 1
            recovery_replan_count += int(payload.get("strategy") == "replan")
        elif event_type == "recovery_exhausted":
            recovery_exhausted_count += 1
        elif event_type == "skill_discovered":
            skill_discovered_count += 1
        elif event_type == "skill_selected":
            skill_selected_count += 1
        elif event_type == "skill_loaded":
            skill_loaded_count += 1
        elif event_type == "skill_reference_loaded":
            skill_reference_loaded_count += 1
    return TrialMetrics(
        steps=run_result.steps_taken,
        total_tokens=run_result.total_tokens,
        provider_usage=run_result.usage.to_dict(),
        wall_time_seconds=wall_time_seconds,
        tool_call_count=tool_call_count,
        test_attempt_count=test_attempt_count,
        completion_rejection_count=completion_rejection_count,
        reflection_count=reflection_count,
        file_read_count=file_read_count,
        shell_call_count=shell_call_count,
        plan_created_count=plan_created_count,
        plan_revision_count=plan_revision_count,
        plan_step_completed_count=plan_step_completed_count,
        planning_skipped=planning_skipped,
        failure_classified_count=failure_classified_count,
        recovery_selected_count=recovery_selected_count,
        recovery_replan_count=recovery_replan_count,
        recovery_exhausted_count=recovery_exhausted_count,
        skill_discovered_count=skill_discovered_count,
        skill_selected_count=skill_selected_count,
        skill_loaded_count=skill_loaded_count,
        skill_reference_loaded_count=skill_reference_loaded_count,
        mcp_tool_discovered_count=mcp_tool_discovered_count,
        mcp_tool_call_count=mcp_tool_call_count,
        mcp_tool_failure_count=mcp_tool_failure_count,
    )
