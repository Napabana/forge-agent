"""Shared terminal observer for EventLog events across product entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field

import click

from agent.task import Event, EventType


@dataclass
class RunEventRenderer:
    """Render one run's append-only events without affecting execution."""

    preview_lines: int = 5
    compact: bool = False
    show_task: bool = True
    show_reasoning: bool = False
    _pending_message: str = field(default="", init=False)
    _streamed_text: list[str] = field(default_factory=list, init=False)

    def reset(self) -> None:
        self._pending_message = ""
        self._streamed_text.clear()

    def record_streamed_text(self, text: str) -> None:
        self._streamed_text.append(text)

    def __call__(self, event: Event) -> None:
        payload = event.payload
        event_type = event.event_type

        if event_type is EventType.TASK_START and self.show_task:
            task = payload.get("task", {})
            click.echo(click.style(f"\n{'─' * 60}", bold=True))
            click.echo(click.style(f"Task: {str(task.get('description', ''))[:80]}", bold=True))
            click.echo(f"Repo: {task.get('repo_path', '')}")
            return

        if event_type is EventType.ACTION:
            step = payload.get("step", "?")
            action = payload.get("action", {})
            action_type = action.get("action_type", "unknown")
            tool_call = action.get("tool_call") or {}
            line = f"[Step {step}] action={action_type}"
            if tool_call:
                line += f" tool={tool_call.get('name', '')}"
                key = _key_param(tool_call.get("params", {}))
                if key:
                    line += f" params={key[:80]}{'...' if len(key) > 80 else ''}"
            click.echo(click.style(line, fg="cyan"))
            thought = (action.get("thought") or "").strip()
            if self.show_reasoning and thought and thought != "(no thought)":
                click.echo(click.style(f"  ↳ {thought[:300]}", dim=True))
            if action_type == "finish":
                self._pending_message = action.get("message") or ""
            return

        if event_type is EventType.OBSERVATION:
            observation = payload.get("observation", {})
            status = observation.get("status", "unknown")
            tool = observation.get("tool_name", "unknown")
            color = "green" if status == "success" else "red"
            click.echo(click.style(f"  Observation [{tool}]: {status}", fg=color))
            detail = observation.get("error") or observation.get("output") or ""
            if detail and (not self.compact or status != "success"):
                lines = str(detail).strip().splitlines()
                for line in lines[:self.preview_lines]:
                    click.echo(click.style(f"    {line}", dim=True))
                if len(lines) > self.preview_lines:
                    click.echo(click.style(
                        f"    ... ({len(lines) - self.preview_lines} more lines)",
                        dim=True,
                    ))
            return

        if event_type is EventType.PLAN_CREATED:
            plan = payload.get("plan", {})
            click.echo(click.style(
                f"Plan v{plan.get('version', '?')}: {plan.get('goal', '')}",
                fg="magenta",
            ))
            return
        if event_type is EventType.PLAN_REVISED:
            click.echo(click.style(
                "Plan revised "
                f"v{payload.get('previous_version', '?')}→v{payload.get('new_version', '?')}: "
                f"{payload.get('reason', '')}",
                fg="magenta",
            ))
            return
        if event_type is EventType.PLAN_REJECTED:
            click.echo(click.style(
                f"Plan rejected: {payload.get('error', '')}", fg="yellow",
            ))
            return
        if event_type is EventType.SKILL_LOADED:
            click.echo(click.style(
                f"Skill loaded: {payload.get('skill', '')}", fg="magenta",
            ))
            return
        if event_type is EventType.COMPLETION_REJECTED:
            click.echo(click.style(
                f"Finish rejected [{payload.get('code', 'unknown')}]: "
                f"{payload.get('detail', '')}",
                fg="yellow",
            ))
            return
        if event_type is EventType.RECOVERY_SELECTED:
            click.echo(click.style(
                f"Recovery [{payload.get('category', 'unknown')}] "
                f"{payload.get('strategy', 'unknown')} "
                f"({payload.get('attempt', '?')}/{payload.get('max_attempts', '?')}): "
                f"{payload.get('reason', '')}",
                fg="yellow",
            ))
            return
        if event_type is EventType.RECOVERY_EXHAUSTED:
            click.echo(click.style(
                f"Recovery exhausted [{payload.get('category', 'unknown')}]: "
                f"{payload.get('reason', '')}",
                fg="red",
            ))
            return

        if event_type is EventType.ACCEPTANCE:
            status = payload.get("acceptance_status") or payload.get("status", "unknown")
            click.echo(click.style(f"Acceptance: {status}", fg=_status_color(status)))
            return

        if event_type is EventType.DELIVERY:
            status = payload.get("delivery_status") or payload.get("status", "unknown")
            click.echo(click.style(f"Delivery: {status}", fg=_status_color(status)))
            return

        if event_type is EventType.CONTEXT_COMPACTION_STARTED:
            click.echo(click.style("[Context compaction]", dim=True))
            return
        if event_type is EventType.CONTEXT_COMPACTION_FAILED:
            click.echo(click.style("[Context compaction failed; using fallback]", fg="yellow"))
            return
        if event_type is EventType.REFLECTION:
            click.echo(click.style(
                f"Reflection: {payload.get('reason', '')}", fg="yellow",
            ))
            return
        if event_type is EventType.TASK_COMPLETE:
            streamed = "".join(self._streamed_text).strip()
            message = self._pending_message.strip()
            if message and message != streamed:
                click.echo(message)
            click.echo(click.style("Task complete", fg="green", bold=True))
            return
        if event_type in (EventType.TASK_FAILED, EventType.TASK_INCOMPLETE):
            reason = payload.get("reason") or payload.get("summary") or ""
            click.echo(click.style(f"Task {event_type.value}: {reason}", fg="red", bold=True))


def _key_param(params: dict) -> str:
    for name in ("cmd", "path", "pattern", "symbol", "message"):
        value = params.get(name)
        if value:
            return str(value)
    return ""


def _status_color(status: str) -> str:
    if status in {"passed", "delivered", "success"}:
        return "green"
    if status in {"skipped", "not_requested"}:
        return "yellow"
    return "red"
