"""
agent/event_log.py

Append-only JSONL 事件日志。
整个 agent 运行过程的完整记录，支持：
- 实时写入（每条 event 立刻 flush 到磁盘）
- 确定性回放（replay 还原完整事件序列）
- 按 task_id 隔离（每次运行一个独立文件）
- 人类可读（JSONL 格式，可直接 cat / tail -f）

Trace v2 约束：
- 所有新写入事件都带同一 run/schema correlation 元数据
- 有持续时间的模型、工具、上下文操作使用 span_id/parent_span_id
- 敏感信息在最终 JSONL 写盘边界统一脱敏，不依赖各 Tool 自己处理
- 旧 JSONL 只读兼容，不迁移、不重写
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from agent.task import Action, Event, EventType, Observation, RunResult, Task
from agent.trace_v2 import (
    TRACE_SCHEMA_VERSION,
    current_trace_context,
    redact_trace_value,
)
from llm.usage import SessionUsage, TokenUsage

logger = logging.getLogger(__name__)


_TRACE_STARTED = {
    EventType.PREPARE_NEXT_TURN_STARTED,
    EventType.LLM_CALL_STARTED,
    EventType.TOOL_EXECUTION_STARTED,
    EventType.CONTEXT_COMPACTION_STARTED,
}
_TRACE_FAILED = {
    EventType.PREPARE_NEXT_TURN_FAILED,
    EventType.LLM_CALL_FAILED,
    EventType.TOOL_EXECUTION_FAILED,
    EventType.CONTEXT_COMPACTION_FAILED,
}
_TRACE_FINISHED = {
    EventType.PREPARE_NEXT_TURN_FINISHED,
    EventType.LLM_CALL_FINISHED,
    EventType.TOOL_EXECUTION_FINISHED,
    EventType.CONTEXT_COMPACTED,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _span_type(event_type: EventType) -> str:
    if event_type.name.startswith("LLM_CALL_"):
        return "model"
    if event_type.name.startswith("TOOL_EXECUTION_"):
        return "tool"
    if event_type.name.startswith("CONTEXT_") or event_type.name.startswith("PREPARE_NEXT_TURN_"):
        return "context"
    if event_type.name.startswith("PLAN_") or event_type is EventType.PLANNING_SKIPPED:
        return "planning"
    if event_type.name.startswith("FAILURE_") or event_type.name.startswith("RECOVERY_"):
        return "recovery"
    if event_type.name.startswith("SKILL_"):
        return "skill"
    if event_type.name.startswith("MCP_"):
        return "mcp"
    if event_type is EventType.COMPLETION_REJECTED:
        return "completion"
    if event_type is EventType.ACCEPTANCE:
        return "acceptance"
    if event_type is EventType.DELIVERY:
        return "delivery"
    if event_type is EventType.RUN_TERMINATED:
        return "run"
    return "event"


def _span_family(event_type: EventType) -> str:
    """Return an operation family for pairing started/finished lifecycle events.

    ``prepare_next_turn`` can contain a nested compaction span. Both are context
    spans, so keying only by ``span_type`` would make deterministic compaction
    accidentally reuse its parent prepare span.
    """

    if event_type.name.startswith("PREPARE_NEXT_TURN_"):
        return "prepare"
    if event_type.name.startswith("LLM_CALL_"):
        return "model"
    if event_type.name.startswith("TOOL_EXECUTION_"):
        return "tool"
    if event_type.name.startswith("CONTEXT_COMPACTION_") or event_type is EventType.CONTEXT_COMPACTED:
        return "compaction"
    return _span_type(event_type)


def _span_status(event_type: EventType) -> str:
    if event_type in _TRACE_STARTED:
        return "started"
    if event_type in _TRACE_FAILED:
        return "error"
    if event_type is EventType.LLM_CALL_RETRY:
        return "retry"
    if event_type in _TRACE_FINISHED:
        return "ok"
    return "event"


class EventLog:
    """JSONL append-only event log with Trace v2 correlation and redaction."""

    def __init__(
        self,
        path: Path,
        task_id: str | None = None,
        session_id: str | None = None,
        entrypoint: str | None = None,
        *,
        run_id: str | None = None,
        run_span_id: str | None = None,
        started_at: str | None = None,
    ) -> None:
        self._path = path
        self._file = open(path, "a", encoding="utf-8")
        self._task_id = task_id
        self._session_id = session_id
        self._entrypoint = entrypoint or "direct"
        self._run_id = run_id or path.stem
        self._run_span_id = run_span_id or uuid.uuid4().hex[:16]
        self._started_at = started_at or _utc_now()
        self._on_append: "Callable[[Event], None] | None" = None
        self._tool_args_by_step: dict[int, dict] = {}
        self._active_spans: dict[tuple[str, int], str] = {}

    @classmethod
    def create(
        cls,
        task: Task,
        log_dir: str = "./logs",
        session_id: str | None = None,
        entrypoint: str | None = None,
    ) -> "EventLog":
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{task.task_id}_{timestamp}.jsonl"
        inherited = current_trace_context()
        return cls(
            log_path / filename,
            task_id=task.task_id,
            session_id=(
                session_id if session_id is not None else inherited.session_id
            ),
            entrypoint=entrypoint or inherited.entrypoint,
        )

    @classmethod
    def open_existing(
        cls,
        path: str | Path,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        entrypoint: str | None = None,
    ) -> "EventLog":
        """Open an existing JSONL for append while preserving v2 correlation.

        Old JSONL files may not carry v2 metadata. In that case correlation falls
        back to the filename/task id and a new run span is created only for the
        appended event; the historical lines are never rewritten.
        """

        resolved = Path(path)
        context: dict = {}
        if resolved.exists():
            try:
                with open(resolved, encoding="utf-8") as source:
                    for line in source:
                        line = line.strip()
                        if not line:
                            continue
                        raw = json.loads(line)
                        payload = raw.get("payload") or {}
                        context = {
                            "task_id": raw.get("task_id"),
                            "session_id": payload.get("session_id"),
                            "entrypoint": payload.get("entrypoint"),
                            "run_id": payload.get("run_id"),
                            "run_span_id": payload.get("run_span_id") or (
                                payload.get("span_id")
                                if raw.get("event_type") == EventType.TASK_START.value
                                else None
                            ),
                            "started_at": payload.get("started_at") or raw.get("timestamp"),
                        }
                        break
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                context = {}
        return cls(
            resolved,
            task_id=task_id or context.get("task_id"),
            session_id=session_id if session_id is not None else context.get("session_id"),
            entrypoint=entrypoint or context.get("entrypoint"),
            run_id=context.get("run_id") or resolved.stem,
            run_span_id=context.get("run_span_id"),
            started_at=context.get("started_at"),
        )

    def configure_trace(
        self,
        *,
        entrypoint: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Bind product-level context before the first event is written."""
        if entrypoint:
            self._entrypoint = entrypoint
        if session_id is not None:
            self._session_id = session_id

    # ------------------------------------------------------------------
    # Semantic write helpers
    # ------------------------------------------------------------------

    def log_task_start(self, task: Task) -> None:
        self._append(Event(
            event_type=EventType.TASK_START,
            task_id=task.task_id,
            payload={
                "task": task.to_dict(),
                "span_id": self._run_span_id,
                "parent_span_id": None,
                "span_type": "run",
                "status": "started",
                "started_at": self._started_at,
            },
        ))

    def log_action(
        self,
        step: int,
        action: Action,
        raw_content: str = "",
        usage: TokenUsage | None = None,
    ) -> str:
        if action.tool_call is not None:
            self._tool_args_by_step[step] = dict(action.tool_call.params)
        event = Event(
            event_type=EventType.ACTION,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "action": action.to_dict(),
                "raw_content": raw_content,
                "usage": usage.to_dict() if usage else None,
            },
        )
        self._append(event)
        return event.event_id

    def log_observation(self, step: int, observation: Observation) -> str:
        event = Event(
            event_type=EventType.OBSERVATION,
            task_id=self._current_task_id,
            payload={"step": step, "observation": observation.to_dict()},
        )
        self._append(event)
        return event.event_id

    def log_trace(
        self,
        event_type: EventType,
        step: int,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        session_id: str | None = None,
        **details,
    ) -> str:
        """Append one Trace v2 lifecycle event.

        A lifecycle span reuses the same ``span_id`` between start/retry/finish.
        Context compaction historically did not pass the start span onward, so
        EventLog keeps a tiny per-step active-span table to correlate it without
        changing the compaction policy API.
        """

        span_kind = _span_type(event_type)
        active_key = (_span_family(event_type), step)
        if span_id is None and event_type in {
            EventType.CONTEXT_COMPACTION_FAILED,
            EventType.CONTEXT_COMPACTED,
        }:
            span_id = self._active_spans.get(active_key)
        span_id = span_id or uuid.uuid4().hex[:16]
        if event_type in _TRACE_STARTED:
            self._active_spans[active_key] = span_id

        if parent_span_id is None and span_id != self._run_span_id:
            parent_span_id = self._run_span_id

        payload = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            # Compatibility alias used by existing B1/B2/test readers.
            "schema_version": TRACE_SCHEMA_VERSION,
            "run_id": self._run_id,
            "run_span_id": self._run_span_id,
            "entrypoint": self._entrypoint,
            "session_id": session_id if session_id is not None else self._session_id,
            "turn_id": f"{self._run_id}:{step}",
            "step_id": step,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "span_type": span_kind,
            "status": _span_status(event_type),
            **details,
        }
        if event_type is EventType.LLM_CALL_STARTED:
            payload.setdefault("model_call_id", span_id)
        elif event_type.name.startswith("LLM_CALL_"):
            payload.setdefault("model_call_id", span_id)
        elif event_type.name.startswith("TOOL_EXECUTION_"):
            payload.setdefault("tool_execution_id", span_id)
            if event_type is EventType.TOOL_EXECUTION_STARTED:
                payload.setdefault("arguments", self._tool_args_by_step.get(step, {}))
        elif event_type.name.startswith("CONTEXT_COMPACTION_") or event_type is EventType.CONTEXT_COMPACTED:
            payload.setdefault("compaction_id", span_id)

        try:
            self._append(Event(
                event_type=event_type,
                task_id=self._current_task_id,
                payload=payload,
            ))
        except Exception as exc:  # trace must not replace the Agent result
            logger.warning("Trace write failed for %s: %s", event_type.value, exc)

        if event_type in _TRACE_FAILED or event_type in _TRACE_FINISHED:
            if self._active_spans.get(active_key) == span_id:
                self._active_spans.pop(active_key, None)
        return span_id

    def log_reflection(self, step: int, reason: str, prompt: str) -> None:
        self._append(Event(
            event_type=EventType.REFLECTION,
            task_id=self._current_task_id,
            payload={"step": step, "reason": reason, "prompt": prompt},
        ))

    def log_loop_detected(
        self,
        step: int,
        *,
        severity: str,
        period: int,
        repeats: int,
        occurrence: int,
        action_pattern: list[str],
    ) -> None:
        self._append(Event(
            event_type=EventType.LOOP_DETECTED,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "severity": severity,
                "period": period,
                "repeats": repeats,
                "occurrence": occurrence,
                "action_pattern": action_pattern,
                "progress": False,
            },
        ))

    def log_completion_rejected(self, step: int, code: str, detail: str) -> str:
        span_id = uuid.uuid4().hex[:16]
        event = Event(
            event_type=EventType.COMPLETION_REJECTED,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "code": code,
                "detail": detail,
                "span_id": span_id,
                "parent_span_id": self._run_span_id,
                "span_type": "completion",
                "status": "rejected",
                "completion_id": span_id,
            },
        )
        self._append(event)
        return event.event_id

    def log_task_incomplete(
        self,
        steps: int,
        reason: str,
        *,
        termination_reason: str,
        resource_reason: str | None = None,
    ) -> None:
        self._append(Event(
            event_type=EventType.TASK_INCOMPLETE,
            task_id=self._current_task_id,
            payload={
                "steps": steps,
                "reason": reason,
                "termination_reason": termination_reason,
                "resource_reason": resource_reason,
            },
        ))

    def log_task_complete(self, steps: int, summary: str) -> None:
        self._append(Event(
            event_type=EventType.TASK_COMPLETE,
            task_id=self._current_task_id,
            payload={"steps": steps, "summary": summary},
        ))

    def log_task_failed(self, steps: int, reason: str) -> None:
        self._append(Event(
            event_type=EventType.TASK_FAILED,
            task_id=self._current_task_id,
            payload={"steps": steps, "reason": reason},
        ))

    def log_run_termination(self, result: RunResult) -> None:
        """Write the normalized Runner-visible terminal record."""
        self._append(Event(
            event_type=EventType.RUN_TERMINATED,
            task_id=result.task_id,
            payload={
                "step": result.steps_taken,
                "span_id": self._run_span_id,
                "parent_span_id": None,
                "span_type": "run",
                "status": result.status.value,
                "started_at": self._started_at,
                "finished_at": _utc_now(),
                "termination_reason": result.termination_reason,
                "resource_reason": result.resource_reason,
                "error": result.error,
            },
        ))

    def log_run_exception(
        self,
        *,
        task_id: str,
        error: BaseException,
        steps: int = 0,
        termination_reason: str = "infrastructure_error",
    ) -> None:
        """Trace an exception that escaped the Agent before re-raising it."""
        self._append(Event(
            event_type=EventType.RUN_TERMINATED,
            task_id=task_id,
            payload={
                "step": steps,
                "span_id": self._run_span_id,
                "parent_span_id": None,
                "span_type": "run",
                "status": "failed",
                "started_at": self._started_at,
                "finished_at": _utc_now(),
                "termination_reason": termination_reason,
                "resource_reason": None,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        ))

    def log_acceptance(self, result: RunResult, *, requested: bool) -> None:
        raw_status = result.acceptance_status
        status = raw_status
        if not requested or raw_status == "not_requested":
            status = "skipped"
        self.log_trace(
            EventType.ACCEPTANCE,
            result.steps_taken,
            requested=requested,
            status=status,
            acceptance_status=raw_status,
            error=result.acceptance_error,
        )

    def log_delivery(
        self,
        *,
        steps: int,
        requested: bool,
        delivery_status: str,
        error: str | None = None,
        **metadata,
    ) -> None:
        if not requested or delivery_status == "not_requested":
            status = "skipped"
        elif delivery_status == "delivered":
            status = "delivered"
        elif delivery_status in {"blocked_agent", "blocked_acceptance"}:
            status = "skipped"
        else:
            status = "failed"
        self.log_trace(
            EventType.DELIVERY,
            steps,
            requested=requested,
            status=status,
            delivery_status=delivery_status,
            error=error,
            **metadata,
        )

    # --- worktree / permission audit events ---

    def log_task_claimed(self, task_id: str, owner: str) -> None:
        self._append(Event(
            event_type=EventType.TASK_CLAIMED,
            task_id=task_id,
            payload={"task_id": task_id, "owner": owner},
        ))

    def log_worktree_created(
        self, task_id: str, name: str, path: str, base: str = "HEAD"
    ) -> None:
        self._append(Event(
            event_type=EventType.WORKTREE_CREATED,
            task_id=task_id,
            payload={"name": name, "path": path, "base": base},
        ))

    def log_worktree_removed(
        self, task_id: str, name: str, path: str, reason: str
    ) -> None:
        self._append(Event(
            event_type=EventType.WORKTREE_REMOVED,
            task_id=task_id,
            payload={"name": name, "path": path, "reason": reason},
        ))

    def log_worktree_retained(
        self,
        task_id: str,
        branch: str,
        path: str,
        *,
        reason: str,
        changed_files: list[str],
    ) -> None:
        self._append(Event(
            event_type=EventType.WORKTREE_RETAINED,
            task_id=task_id,
            payload={
                "branch": branch,
                "path": path,
                "reason": reason,
                "changed_files": changed_files,
            },
        ))

    def log_permission_decision(
        self,
        task_id: str,
        tool: str,
        decision: str,
        reason: str,
        params: dict,
    ) -> None:
        self._append(Event(
            event_type=EventType.PERMISSION_DECISION,
            task_id=task_id,
            payload={
                "tool": tool,
                "decision": decision,
                "reason": reason,
                "params": params,
            },
        ))

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def replay(self) -> list[Event]:
        if not self._file.closed:
            self._file.flush()
        events: list[Event] = []
        with open(self._path, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                events.append(Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                ))
        return events

    def iter_events(self) -> Iterator[Event]:
        if not self._file.closed:
            self._file.flush()
        with open(self._path, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                yield Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                )

    def get_actions(self) -> list[Action]:
        from agent.task import ActionType, ToolCall

        actions: list[Action] = []
        for event in self.iter_events():
            if event.event_type != EventType.ACTION:
                continue
            raw_action = event.payload["action"]
            raw_tc = raw_action.get("tool_call")
            tool_call = None
            if raw_tc:
                tool_call = ToolCall(
                    name=raw_tc["name"],
                    params=raw_tc["params"],
                )
            actions.append(Action(
                action_type=ActionType(raw_action["action_type"]),
                thought=raw_action["thought"],
                tool_call=tool_call,
                message=raw_action.get("message"),
            ))
        return actions

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_span_id(self) -> str:
        return self._run_span_id

    @property
    def entrypoint(self) -> str:
        return self._entrypoint

    @property
    def _current_task_id(self) -> str:
        if self._task_id:
            return self._task_id
        return self._path.stem.split("_")[0]

    def on_append(self, cb: "Callable[[Event], None] | None") -> None:
        self._on_append = cb

    # ------------------------------------------------------------------
    # Internal write boundary
    # ------------------------------------------------------------------

    def _enrich_payload(self, event: Event) -> dict:
        payload = dict(event.payload)
        payload.setdefault("trace_schema_version", TRACE_SCHEMA_VERSION)
        payload.setdefault("schema_version", TRACE_SCHEMA_VERSION)
        payload.setdefault("run_id", self._run_id)
        payload.setdefault("run_span_id", self._run_span_id)
        payload.setdefault("entrypoint", self._entrypoint)
        payload.setdefault("session_id", self._session_id)

        step = payload.get("step_id")
        if step is None:
            step = payload.get("step")
        if step is None:
            step = payload.get("steps")
        if step is not None:
            payload.setdefault("step_id", step)
            payload.setdefault("turn_id", f"{self._run_id}:{step}")

        if event.event_type is EventType.TASK_START:
            payload.setdefault("span_id", self._run_span_id)
            payload.setdefault("parent_span_id", None)
            payload.setdefault("span_type", "run")
            payload.setdefault("status", "started")
            payload.setdefault("started_at", self._started_at)
        elif "parent_span_id" not in payload:
            payload["parent_span_id"] = self._run_span_id

        return redact_trace_value(payload)

    def _append(self, event: Event) -> None:
        event.payload = self._enrich_payload(event)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()
        if self._on_append is not None:
            try:
                self._on_append(event)
            except Exception:  # observer failure cannot change execution
                pass

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"EventLog(path={self._path})"


def summarize_run(log: EventLog) -> dict:
    """Read a completed log and return backward-compatible summary statistics."""
    events = log.replay()

    stats = {
        "total_events": len(events),
        "actions": 0,
        "reflections": 0,
        "tool_calls": {},
        "observations_ok": 0,
        "observations_err": 0,
        "final_status": None,
        "usage": None,
        "trace": {
            "prepare_calls": 0,
            "llm_calls": 0,
            "llm_retries": 0,
            "tool_calls": 0,
            "duration_ms": {"prepare": 0.0, "llm": 0.0, "tool": 0.0},
            "errors": {},
        },
    }

    usage = SessionUsage()

    for event in events:
        if event.event_type == EventType.ACTION:
            stats["actions"] += 1
            raw_usage = event.payload.get("usage")
            if raw_usage:
                usage.record(TokenUsage(**{
                    key: value for key, value in raw_usage.items()
                    if key in TokenUsage.__dataclass_fields__
                }))
            tc = event.payload["action"].get("tool_call")
            if tc:
                name = tc["name"]
                stats["tool_calls"][name] = stats["tool_calls"].get(name, 0) + 1

        elif event.event_type == EventType.OBSERVATION:
            obs = event.payload["observation"]
            if obs["status"] == "success":
                stats["observations_ok"] += 1
            else:
                stats["observations_err"] += 1

        elif event.event_type == EventType.REFLECTION:
            stats["reflections"] += 1

        elif event.event_type in (
            EventType.TASK_COMPLETE,
            EventType.TASK_FAILED,
            EventType.TASK_INCOMPLETE,
        ):
            stats["final_status"] = event.event_type.value

        if event.event_type in (
            EventType.PREPARE_NEXT_TURN_FINISHED,
            EventType.PREPARE_NEXT_TURN_FAILED,
        ):
            stats["trace"]["prepare_calls"] += 1
            stats["trace"]["duration_ms"]["prepare"] += event.payload.get("duration_ms", 0)
        elif event.event_type in (EventType.LLM_CALL_FINISHED, EventType.LLM_CALL_FAILED):
            stats["trace"]["llm_calls"] += 1
            stats["trace"]["duration_ms"]["llm"] += event.payload.get("duration_ms", 0)
        elif event.event_type == EventType.LLM_CALL_RETRY:
            stats["trace"]["llm_retries"] += 1
        elif event.event_type in (
            EventType.TOOL_EXECUTION_FINISHED,
            EventType.TOOL_EXECUTION_FAILED,
        ):
            stats["trace"]["tool_calls"] += 1
            stats["trace"]["duration_ms"]["tool"] += event.payload.get("duration_ms", 0)

        error_type = event.payload.get("error_type")
        if error_type:
            errors = stats["trace"]["errors"]
            errors[error_type] = errors.get(error_type, 0) + 1

    stats["usage"] = usage.to_dict()
    return stats
