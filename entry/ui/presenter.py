"""Read-only projections of execution events. Unlisted trace spans are verbose-only."""
from dataclasses import dataclass

from agent.task import Event, EventType as E
from agent.trace_v2 import redact_trace_value
from config.schema import UIConfig


@dataclass(frozen=True)
class TerminalEvent:
    title: str
    style: str = "text.secondary"
    detail: str = ""
    phase: bool = False
    rich_title: str = ""


# Select fields explicitly. Trace metadata can contain large model and tool payloads.
_ROWS = {
    E.PREPARE_NEXT_TURN_STARTED: ("Preparing context", "status.running", (), True),
    E.LLM_CALL_STARTED: ("Waiting for model", "status.running", ("model",), True),
    E.LLM_CALL_FINISHED: ("Model finished", "status.success", ("duration_ms",), False),
    E.LLM_CALL_RETRY: ("Model retry", "action.recovery", ("attempt", "error_type", "retry_after"), False),
    E.LLM_CALL_FAILED: ("Model failed", "status.error", ("error_type", "error", "status_code"), False),
    E.TOOL_EXECUTION_STARTED: ("Executing tool", "status.running", ("tool_name",), True),
    E.TOOL_EXECUTION_FINISHED: ("Tool finished", "status.success", ("tool_name", "duration_ms"), True),
    E.TOOL_EXECUTION_FAILED: ("Tool failed", "status.error", ("tool_name", "duration_ms", "error"), False),
    E.PLAN_STEP_STARTED: ("Plan step started", "action.plan", ("step_id", "reason"), False),
    E.PLAN_STEP_COMPLETED: ("Plan step completed", "status.success", ("step_id", "reason"), False),
    E.MCP_SERVER_CAPABILITIES: ("MCP capabilities", "action.tool", ("server_id", "server_name", "tool_count"), False),
    E.CONTEXT_COMPACTION_STARTED: ("[Context compaction]", "action.plan", (), False),
    E.CONTEXT_COMPACTION_FAILED: ("[Context compaction failed; using fallback]", "status.warning", ("error",), False),
    E.CONTEXT_COMPACTED: ("Context compacted", "action.plan", ("before_tokens", "after_tokens", "checkpoint_id"), False),
    E.WORKERS_SUMMARY: ("Workers", "action.plan", ("worker_count", "worker_failure_count", "worker_usage"), False),
    E.RUN_TERMINATED: ("Run terminated", "text.secondary", ("termination_reason", "resource_reason"), False),
    E.PERMISSION_DECISION: ("Permission", "status.warning", ("tool", "decision", "reason"), False),
    E.RECOVERY_BLOCKED: ("Recovery blocked", "status.warning", ("category", "reason"), False),
}


def present(event: Event, config: UIConfig, *, verbose=False, reasoning=False) -> TerminalEvent | None:
    kind, p = event.event_type, redact_trace_value(event.payload)
    if not isinstance(kind, E):
        return None
    if kind in (E.WORKER_FINISHED, E.WORKER_PARTIAL_READY):
        if p.get("partial_already_notified"):
            return None
        status = p.get("worker_status", "unknown")
        worker = p.get("worker_id", "")
        label = "partial result" if kind == E.WORKER_PARTIAL_READY else status
        return TerminalEvent(f"Worker {label} · {p.get('worker_role', '')} · {worker}",
                             "status.success" if status == "success" else "status.warning")
    if kind == E.ACTION:
        action = p.get("action", {})
        name = action.get("action_type", "unknown")
        call = action.get("tool_call") or {}
        title = f"[Step {p.get('step', '?')}] action={name}"
        rich_title = f"Step {p.get('step', '?')} · {call.get('name') or name}"
        if call:
            title += f" tool={call.get('name', '')}"
            params = call.get("params", {})
            key = next((str(params[k]) for k in ("cmd", "path", "pattern", "symbol", "message") if params.get(k)), "")
            if key:
                preview = key[:160] + ("…" if len(key) > 160 else "")
                title += f" params={preview}"
                rich_title += f" · {preview}"
        return TerminalEvent(title, "action.tool", (action.get("thought") or "") if reasoning else "",
                             rich_title=rich_title)
    if kind == E.OBSERVATION:
        obs = p.get("observation", {})
        status = obs.get("status", "unknown")
        tool_succeeded = status == "success"
        test_outcome = obs.get("test_outcome")
        status = {"passed": "tests passed", "failed": "tests failed", "no_tests": "no tests collected"}.get(
            test_outcome, status
        )
        style = "status.error" if not tool_succeeded else (
            "status.warning" if test_outcome in {"failed", "no_tests"} else "status.success"
        )
        mode = "all" if verbose else config.tool_output_mode
        lines = str(obs.get("output") or "").strip().splitlines()
        limit = len(lines) if mode == "all" else config.tool_output_preview_lines
        if style == "status.success" and mode == "on_error":
            limit = 1
        preview = lines[:limit] if mode != "none" else []
        if mode != "all":
            preview = [line[:240] + "…" if len(line) > 240 else line for line in preview]
        if preview and len(preview) < len(lines):
            preview.append(f"... ({len(lines) - len(preview)} more lines)")
        if not tool_succeeded and obs.get("error"):
            preview.append(f"Error: {obs['error']}")
        tool = obs.get("tool_name", "unknown")
        return TerminalEvent(f"Observation [{tool}]: {status}", style,
                             "\n".join(preview), rich_title=f"{tool} · {status}")
    if kind == E.TASK_START:
        return TerminalEvent(f"Task: {p.get('task', {}).get('description', '')}", "app.title")
    if kind == E.PLAN_CREATED:
        plan = p.get("plan", {})
        return TerminalEvent(f"Plan v{plan.get('version', '?')}: {plan.get('goal', '')}", "action.plan")
    if kind == E.PLAN_REVISED:
        return TerminalEvent(f"Plan revised v{p.get('previous_version', '?')}→v{p.get('new_version', '?')}: {p.get('reason', '')}", "action.plan")
    if kind == E.COMPLETION_REJECTED:
        return TerminalEvent(f"Finish rejected [{p.get('code', 'unknown')}]: {p.get('detail', '')}", "status.warning")
    if kind == E.RECOVERY_SELECTED:
        return TerminalEvent(f"Recovery [{p.get('category', 'unknown')}] {p.get('strategy', '')} "
                             f"({p.get('attempt', '?')}/{p.get('max_attempts', '?')}): {p.get('reason', '')}", "action.recovery")
    if kind == E.RECOVERY_EXHAUSTED:
        return TerminalEvent(f"Recovery exhausted [{p.get('category', 'unknown')}]: {p.get('reason', '')}", "status.error")
    if kind in (E.DELIVERY, E.ACCEPTANCE):
        status = p.get(kind.value + "_status") or p.get("status", "unknown")
        style = "status.success" if status in ("success", "delivered", "passed") else (
            "text.secondary" if status in ("skipped", "not_requested") else "status.error")
        return TerminalEvent(f"{kind.value.title()}: {status}", style)
    if kind in (E.TASK_COMPLETE, E.TASK_FAILED, E.TASK_INCOMPLETE):
        if kind == E.TASK_COMPLETE:
            return TerminalEvent("Task complete", "status.success")
        return TerminalEvent(f"{kind.value.replace('_', ' ').capitalize()}: {p.get('reason') or p.get('summary', '')}",
                             "status.error" if kind == E.TASK_FAILED else "status.warning")
    if kind in (E.SKILL_LOADED, E.PLAN_REJECTED, E.REFLECTION):
        label, field = {E.SKILL_LOADED: ("Skill loaded", "skill"), E.PLAN_REJECTED: ("Plan rejected", "error"),
                        E.REFLECTION: ("Reflection", "reason")}[kind]
        return TerminalEvent(f"{label}: {p.get(field, '')}", "action.plan")
    row = _ROWS.get(kind)
    if row is None:
        if not verbose or kind == E.MCP_TOOL_DISCOVERED:
            return None
        row = (kind.value.replace("_", " ").capitalize(), "text.secondary", ("step", "reason", "error_type"), False)
    label, style, keys, phase = row
    fields = []
    for key in keys:
        value = p.get(key)
        if value is None or (key == "model" and not config.show_model_metadata):
            continue
        if key == "duration_ms":
            fields.append(f"{value / 1000:.2f}s")
        elif key == "worker_usage":
            if config.show_token_usage and isinstance(value, dict) and value.get("total_tokens"):
                fields.append(f"{value['total_tokens']:,} tokens")
        else:
            fields.append(f"{key}={value}")
    if kind == E.TOOL_EXECUTION_STARTED and p.get("tool_name") == "test":
        label = "Running tests"
    if kind == E.MCP_SERVER_CAPABILITIES and isinstance(p.get("tools"), (list, tuple)):
        fields.append(f"tools={len(p['tools'])}")
    title = " · ".join([label, *fields])
    if phase and p.get("step"):
        title = f"Step {p['step']} · {title}"
    return TerminalEvent(title, style, phase=phase)
