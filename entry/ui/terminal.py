"""Shared inline console for run, chat and event observers."""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from functools import wraps
from threading import RLock

import click
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from agent.task import EventType as E
from agent.trace_v2 import redact_trace_string
from config.schema import UIConfig
from entry.ui.presenter import present
from entry.ui.summary import summary_fields


def safe_text(value):
    # Terminal control sequences from tools/files must never move the real cursor.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(value))


def display_only(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            try:
                return method(self, *args, **kwargs)
            except Exception:
                logging.getLogger(__name__).debug("Terminal display failed", exc_info=True)
                self._stop_live()
                self.channel = None
                self.fragments = []
                self.streamed_answer = []
                self.last_answer = ""
    return wrapped


def make_console(config):
    interactive = (config.mode != "plain" and sys.stdout.isatty() and sys.stderr.isatty()
                   and os.getenv("CI", "").lower() not in ("1", "true", "yes")
                   and os.getenv("TERM") != "dumb")
    if config.mode == "rich" and not interactive:
        click.echo("Rich UI unavailable on this terminal; using plain output.", err=True)
    accent = "blue" if config.theme == "light" else "cyan"
    theme = Theme({"app.title": f"bold {accent}", "text.primary": "default", "text.secondary": "dim",
                   "text.muted": "dim", "status.running": accent, "status.success": "green",
                   "status.warning": "yellow", "status.error": "bold red", "action.tool": accent,
                   "action.plan": "magenta", "action.recovery": "yellow", "diff.added": "green",
                   "diff.removed": "red", "border.default": "dim", "border.focused": accent})
    color = ("standard" if config.theme == "ansi" else "auto") if interactive and "NO_COLOR" not in os.environ else None
    return Console(force_terminal=interactive, color_system=color, theme=theme,
                   highlight=False, markup=False), interactive


class TerminalUI:
    def __init__(self, config=None, *, verbose=False, no_progress=False, chat=False,
                 show_task=False, show_reasoning=False):
        self.config = config or UIConfig()
        self.console, self.rich = make_console(self.config)
        self.progress = self.rich and self.config.show_status_line and not no_progress
        self.verbose = verbose
        self.chat = chat
        self.show_task = show_task
        self.show_reasoning = show_reasoning
        self.lock = RLock()
        self.live = None
        self.reset()

    def reset(self):
        self.close()
        self.seen = set()
        self.tools = Counter()
        self.test_outcomes = Counter()
        self.last_answer = ""
        self.pending_answer = ""
        self.fragments = []
        self.channel = None
        self.phase = ""
        self.started = time.monotonic()
        self.details = {}
        self.durations = {}
        self.streamed_answer = []
        self.loaded = set()
        self.mcp_tools = set()
        self.recoveries = []

    def _print(self, item, **kwargs):
        self.console.print(item, soft_wrap=not self.rich, **kwargs)

    @display_only
    def line(self, value, style="text.primary"):
        self._print(Text(safe_text(value), style))

    @display_only
    def panel(self, title, fields, *, border=True):
        body = Text()
        for key, value in fields.items():
            if value is None or value == "":
                continue
            if body:
                body.append("\n")
            body.append(f"{key}: ", style="text.secondary")
            value = Text(safe_text(redact_trace_string(str(value))))
            if key == "Status":
                style = {"SUCCESS": "status.success", "FAILED": "status.error", "INITIALIZING": "status.running"}
                value.stylize(style.get(value.plain, "status.warning"))
            if key == "Task" and self.rich:
                lines = value.wrap(self.console, max(1, self.console.width - 12))
                if len(lines) > 2:
                    value = Text("\n").join(lines[:2])
                    value.append("…")
            body.append_text(value)
        if self.rich and border:
            self._print(Panel(body, title=title, title_align="left", border_style="border.focused", expand=False))
        else:
            self._print(Text(title, "app.title"))
            if body:
                self._print(body)

    def header(self, title, fields):
        self.panel(title, fields, border=self.verbose)

    def _stop_live(self):
        live, self.live = self.live, None
        if live is not None:
            try:
                live.stop()
            except Exception:
                logging.getLogger(__name__).debug("Terminal cleanup failed", exc_info=True)

    @display_only
    def status(self, phase):
        self.phase = safe_text(phase)
        if self.progress:
            def render():
                return Text(f"{self.phase} · {time.monotonic() - self.started:.1f}s", "status.running")
            if self.live is None:
                if self.config.animations:
                    spinner = Spinner("dots")
                    def animated():
                        spinner.update(text=render())
                        return spinner
                self.live = Live(console=self.console,
                                 get_renderable=animated if self.config.animations else render,
                                 transient=True, refresh_per_second=self.config.live_refresh_per_second,
                                 redirect_stdout=False, redirect_stderr=False)
                self.live.start(refresh=True)
            # A status Live reads the current phase on each refresh.

    @contextmanager
    def activity(self, phase):
        self.reset()
        self.status(phase)
        try:
            yield
        finally:
            self.close()

    def confirm_with(self, callback):
        def confirm(prompt):
            with self.lock:
                previous = self.phase
                self.close()
                self.line("Waiting for permission", "status.warning")
                try:
                    return callback(prompt)
                finally:
                    if previous:
                        self.status(previous)
        return confirm

    def _message(self, text):
        if self.chat and self.rich:
            self._print(Text("FORGE", "app.title"))
        clean = safe_text(text)
        if self.rich and self.config.markdown_enabled:
            self._print(Markdown(clean, code_theme=self.code_theme))
        else:
            self._print(Text(clean))

    @property
    def code_theme(self):
        return "friendly" if self.config.theme == "light" else "ansi_dark"

    @display_only
    def diff(self, patch):
        clean = safe_text(redact_trace_string(patch))
        self._print(Syntax(clean, "diff", theme=self.code_theme, word_wrap=True) if self.rich else Text(clean))

    def _flush(self):
        if self.channel is None:
            return
        text = "".join(self.fragments)
        if self.rich:
            self._stop_live()
            if self.channel == "answer":
                self._message(text)
            else:
                self._print(Text(safe_text(text), "text.muted"))
        elif not text.endswith("\n"):
            self._print(Text(""))
        if self.channel == "answer":
            self.streamed_answer.append(text)
            self.last_answer = "".join(self.streamed_answer).strip()
        self.channel = None
        self.fragments.clear()

    @display_only
    def _stream(self, text, channel):
        if not text:
            return
        if self.channel != channel:
            self._flush()
            self._stop_live()
            self.channel = channel
            self.preview = ""
        self.fragments.append(text)
        style = "text.muted" if channel == "reasoning" else "text.primary"
        if self.rich:
            # Live.stop renders its last frame at full height. Keep that frame on screen.
            self.preview = (self.preview + safe_text(text))[-4096:]
            if self.live is None:
                def tail():
                    width, height = self.console.size
                    lines = Text(self.preview, style).wrap(self.console, max(1, width))
                    return Text("\n").join(lines[-max(1, min(8, height - 2)):])
                self.live = Live(console=self.console, transient=True,
                                 get_renderable=tail,
                                 refresh_per_second=self.config.live_refresh_per_second,
                                 redirect_stdout=False, redirect_stderr=False)
                self.live.start(refresh=True)
        else:
            self._print(Text(safe_text(text), style), end="")

    def on_stream_text(self, text):
        self._stream(text, "answer")

    def on_reasoning_text(self, text):
        self._stream(text, "reasoning")

    @display_only
    def close(self):
        try:
            if getattr(self, "channel", None):
                self._flush()
        finally:
            self._stop_live()

    @display_only
    def __call__(self, event):
        if event.event_id in self.seen:
            return
        self.seen.add(event.event_id)
        kind, payload = event.event_type, event.payload
        if kind == E.LLM_CALL_STARTED:
            self._flush()
            self.streamed_answer.clear()
        if kind == E.ACTION and payload.get("action", {}).get("action_type") == "finish":
            self.pending_answer = payload["action"].get("message") or ""
            if self.chat and not self.verbose:
                return
        elif kind == E.OBSERVATION:
            observation = payload.get("observation", {})
            self.tools[observation.get("status", "unknown")] += 1
            if outcome := observation.get("test_outcome"):
                self.test_outcomes[outcome] += 1
        elif kind in (E.TOOL_EXECUTION_FINISHED, E.TOOL_EXECUTION_FAILED) and payload.get("duration_ms") is not None:
            self.durations[payload.get("step"), payload.get("tool_name")] = payload["duration_ms"]
        elif kind == E.SKILL_LOADED:
            self.loaded.add(payload.get("skill", ""))
        elif kind == E.MCP_TOOL_DISCOVERED:
            self.mcp_tools.add(payload.get("tool_name", ""))
        elif kind == E.LLM_CALL_FINISHED:
            usage = payload.get("usage") or {}
            self.details["input_tokens"] = usage.get("input_tokens") if usage.get("total_tokens") else None
            if self.chat and not self.verbose:
                self._flush()
                self.status("Processing response")
                return
        elif kind == E.CONTEXT_COMPACTED:
            self.details["compact"] = {k: payload[k] for k in ("before_tokens", "after_tokens", "checkpoint_id") if k in payload}
        elif kind in (E.PLAN_CREATED, E.PLAN_REVISED, E.PLAN_STEP_STARTED, E.PLAN_STEP_COMPLETED):
            if payload.get("plan"):
                self.details["plan"] = payload["plan"]
            elif "plan" in self.details:
                # Keep the event read-only, including nested step dictionaries.
                plan = self.details["plan"]
                self.details["plan"] = {**plan, "steps": [
                    {**s, "status": payload.get("step_status")} if s.get("id") == payload.get("step_id") else s
                    for s in plan.get("steps", [])]}
        view = present(event, self.config, verbose=self.verbose, reasoning=self.show_reasoning)
        if view is None or (kind == E.TASK_START and not self.show_task):
            return
        if kind in (E.WORKER_FINISHED, E.WORKER_PARTIAL_READY):
            self.line(view.title, view.style)
            return
        self._flush()
        if kind in (E.RECOVERY_SELECTED, E.RECOVERY_EXHAUSTED, E.RECOVERY_BLOCKED):
            self.recoveries = (self.recoveries + [view.title])[-10:]
        if view.phase and not self.verbose:
            self.status(view.title)
            return
        self._stop_live()
        if kind == E.TASK_COMPLETE:
            self.answer(self.pending_answer or payload.get("summary", ""))
            if self.chat and not self.verbose:
                return
        if (self.chat and not self.verbose and kind == E.RUN_TERMINATED
                and payload.get("termination_reason") == "completion_satisfied"):
            return
        markers = {"action.tool": "▶", "status.success": "✓", "status.warning": "!", "status.error": "✕",
                   "action.plan": "◆", "action.recovery": "↻"}
        prefix = markers.get(view.style, "·") + " " if self.rich else ""
        title = view.rich_title if self.rich and view.rich_title else view.title
        if kind == E.OBSERVATION:
            key = (payload.get("step"), payload.get("observation", {}).get("tool_name"))
            duration = self.durations.pop(key, None)
            if duration is not None:
                title += f" · {duration / 1000:.2f}s"
        self.line(prefix + title, view.style)
        if view.detail:
            self.line(view.detail, "text.secondary")

    @display_only
    def answer(self, text):
        if text and text.strip() != self.last_answer:
            self._message(text)
            self.last_answer = text.strip()

    @display_only
    def finish(self, result, elapsed):
        self.close()
        rows = summary_fields(result, elapsed, self.tools, tests=self.test_outcomes,
                              show_tokens=self.config.show_token_usage)
        summary = getattr(result, "summary", "")
        if summary and summary.strip() != self.last_answer:
            rows["Summary"] = summary
        self.panel("RUN SUMMARY", rows, border=self.verbose)
