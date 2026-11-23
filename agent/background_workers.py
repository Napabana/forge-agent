"""Run-scoped background dispatch and on-demand Worker results."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from agent.coordinator import WorkerProgress, WorkerRequest, WorkerResult, _CombinedCancel
from agent.task import EventType
from context.repository_state import repository_content_fingerprint
from llm.usage import SessionUsage
from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolResult

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class WorkerJob:
    task_id: str
    agent_id: str
    request: WorkerRequest
    token_limit: int
    deadline: float
    future: Future | None = None
    progress: dict | None = None
    activity: WorkerProgress = dataclasses.field(default_factory=WorkerProgress)
    timer: threading.Timer | None = None
    partial: WorkerResult | None = None
    result: WorkerResult | None = None
    source_snapshot: str | None = None
    changed_during_work: bool = False

    def is_set(self) -> bool:
        return time.monotonic() >= self.deadline

    def status(self) -> dict:
        result = self.result or self.partial
        usage = result.token_usage if result is not None else self.activity.snapshot()[0]
        used = usage.get("total_tokens", 0)
        state = result.status if result is not None else "running" if self.progress else "queued"
        if self.result is None and self.partial is not None and self.partial.termination_reason == "budget_wrapup":
            state = "wrapping_up"
        return {"task_id": self.task_id, "role": self.request.role, "status": state, "progress": self.progress,
                "token_limit": self.token_limit, "used_tokens": used, "remaining_tokens": max(0, self.token_limit - used)}


class BackgroundWorkers:
    """Own the pool and publish results for the Lead's next decision."""

    def __init__(self, coordinator, durable) -> None:
        self.coordinator = coordinator
        self.durable = durable
        self.log = None
        self.pool = ThreadPoolExecutor(max_workers=coordinator.max_workers, thread_name_prefix="forge-worker")
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.cancel = _CombinedCancel(coordinator.cancel_event, self.stop)
        self.closed = False
        self.preserve = False
        self.jobs: dict[str, WorkerJob] = {}
        self.batches: dict[str, list[WorkerJob]] = {}

    def attach(self, log) -> None:
        self.log = log
        if self.durable is None or not self.durable.restoring:
            return
        rows = self.durable.store._conn.execute(
            """SELECT event_type, payload_json, delivered_at FROM outbox_events WHERE run_id=?
               AND event_type IN ('worker_dispatch', 'worker_snapshot', 'worker_wrapup', 'worker_partial', 'worker_result')
               ORDER BY seq""",
            (self.durable.run_id,),
        ).fetchall()
        saved_results = {}
        saved_partials = {}
        saved_wrapups = {}
        snapshots = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["event_type"] == "worker_result":
                saved_results[payload["result"]["task_id"]] = payload, row["delivered_at"]
                continue
            if row["event_type"] == "worker_partial":
                saved_partials[payload["result"]["task_id"]] = payload, row["delivered_at"]
                continue
            if row["event_type"] == "worker_wrapup":
                saved_wrapups[payload["result"]["task_id"]] = payload, row["delivered_at"]
                continue
            if row["event_type"] == "worker_snapshot":
                snapshots[payload["task_id"]] = payload["snapshot"]
                continue
            requests = [WorkerRequest(**item) for item in payload["requests"]]
            records = self.coordinator.worker_records(requests, payload["batch_id"])
            jobs = [self._job(record, limit) for record, limit in zip(records, payload["limits"], strict=True)]
            self._remember(payload["batch_id"], jobs)
        with self.condition:
            for job in self.jobs.values():
                partial = saved_partials.get(job.task_id)
                saved = saved_results.get(job.task_id) or partial
                if saved:
                    payload, delivered_at = saved
                    job.result = WorkerResult(**payload["result"])
                    job.source_snapshot = payload["source_snapshot"]
                    job.changed_during_work = payload["changed_during_work"]
                    self._settle(job)
                    if delivered_at is None:
                        self._notify_result(job, job.result, partial=saved is partial, recovered=True)
                else:
                    job.source_snapshot = snapshots.get(job.task_id)
                    if job.task_id in saved_wrapups:
                        payload, delivered_at = saved_wrapups[job.task_id]
                        job.partial = WorkerResult(**payload["result"])
                        job.activity.usage = SessionUsage.from_dict(job.partial.token_usage)
                        job.activity.evidence.extend(job.partial.evidence or [])
                        if delivered_at is None:
                            self._notify_result(job, job.partial, partial=True, recovered=True)
                    self._schedule(job)

    def _journal_write(self, write, *, background: bool) -> None:
        if self.durable is None:
            return
        from durable_runtime.store import DurableRuntimeStore

        run = self.durable

        def transaction(store):
            with store.transaction(run.owner, None) as db:
                write(db)

        if background:
            # SQLite connections stay on the thread that opened them.
            with DurableRuntimeStore(run.store.identity, run.store.base.parent) as store:
                transaction(store)
        else:
            transaction(run.store)

    def _persist(self, kind: str, key: str, payload: dict, *, background: bool = False) -> None:
        if self.durable is None:
            return
        from durable_runtime.run_manager import encode

        run_id = self.durable.run_id
        self._journal_write(lambda db: db.execute(
            """INSERT INTO outbox_events (event_id, run_id, seq, event_type, payload_json, created_at)
               SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM outbox_events WHERE run_id=?""",
            (f"{run_id}:{kind}:{key}", run_id, kind, encode(payload), time.time(), run_id),
        ), background=background)

    def _notify_result(self, job: WorkerJob, result: WorkerResult, *, partial=False, recovered=False) -> None:
        kind = "worker_result"
        if partial:
            kind = "worker_wrapup" if result.termination_reason == "budget_wrapup" else "worker_partial"
        notification_id = f"{self.durable.run_id}:{kind}:{job.task_id}" if self.durable else f"{kind}:{job.task_id}"
        event = EventType.WORKER_PARTIAL_READY if partial else EventType.WORKER_FINISHED
        self.log.log_trace(event, 0, worker_id=job.task_id, worker_role=job.request.role,
                           worker_status=result.status, worker_trace_reference=result.trace_reference,
                           worker_summary=result.summary[:240], worker_token_limit=job.token_limit,
                           worker_used_tokens=result.token_usage.get("total_tokens", 0),
                           worker_evidence_count=len(result.evidence or []),
                           worker_termination_reason=result.termination_reason,
                           worker_resource_reason=result.resource_reason,
                           partial_already_notified=not partial and job.partial is not None and result.status != "success",
                           notification_id=notification_id, recovered=recovered)
        try:
            self._journal_write(lambda db: db.execute(
                "UPDATE outbox_events SET delivered_at=? WHERE event_id=?",
                (time.time(), notification_id),
            ), background=not recovered)
        except Exception as error:
            logger.warning("Could not record Worker notification delivery (%s)", type(error).__name__)

    def _job(self, record, limit: int) -> WorkerJob:
        task_id, agent_id, request = record
        return WorkerJob(task_id, agent_id, request, limit, time.monotonic() + self.coordinator.timeout_seconds)

    def _remember(self, batch_id: str, jobs: list[WorkerJob]) -> None:
        self.batches[batch_id] = jobs
        self.jobs.update((job.task_id, job) for job in jobs)
        self.coordinator.delegation_count += 1
        self.coordinator.remaining_tokens -= sum(job.token_limit for job in jobs)

    def submit(self, requests: list[WorkerRequest], *, operation_id: str | None = None) -> list[dict]:
        batch_id = operation_id or uuid.uuid4().hex
        with self.condition:
            if batch_id in self.batches:
                jobs = self.batches[batch_id]
                if [job.request for job in jobs] != requests:
                    raise ValueError("saved Worker dispatch arguments differ")
                return [job.status() for job in jobs]
            if self.closed or self.cancel.is_set():
                raise RuntimeError("Worker pool is closing")
            limits = self.coordinator.allocations(requests)
            records = self.coordinator.worker_records(requests, batch_id)
            jobs = [self._job(record, limit) for record, limit in zip(records, limits, strict=True)]
            self._persist("worker_dispatch", batch_id, {
                "batch_id": batch_id, "requests": [dataclasses.asdict(request) for request in requests], "limits": limits,
            })
            self._remember(batch_id, jobs)
            receipt = [job.status() for job in jobs]
            for job in jobs:
                self.log.log_trace(EventType.WORKER_DISPATCHED, 0, worker_id=job.task_id,
                                   worker_role=job.request.role, dispatch_id=batch_id, worker_token_limit=job.token_limit)
                self._schedule(job)
            return receipt

    def _schedule(self, job: WorkerJob) -> None:
        job.future = self.pool.submit(self._run, job)
        job.timer = threading.Timer(max(0, job.deadline - time.monotonic()), self._deadline_reached, args=(job,))
        job.timer.daemon = True
        job.timer.start()
        job.future.add_done_callback(lambda future: self._complete(job, future))

    def _deadline_reached(self, job: WorkerJob) -> None:
        with self.condition:
            if self.closed or self.cancel.is_set() or job.future.done() or job.result is not None:
                return
            self._publish_partial(job, timed_out=True)

    def _publish_partial(self, job: WorkerJob, *, timed_out: bool) -> None:
        usage, evidence = job.activity.snapshot()
        reason = "timeout" if timed_out else "budget_wrapup"
        summary = ("Worker deadline elapsed." if timed_out else "Worker is at the token wrap-up threshold.")
        result = WorkerResult(job.task_id, job.agent_id, job.request.role,
                              "timed_out" if timed_out else "incomplete",
                              summary + " Saved observations describe progress.", [], [], usage,
                              max(0.0, self.coordinator.timeout_seconds - (job.deadline - time.monotonic())),
                              None, self.coordinator.parent_task_id, False, token_limit=job.token_limit,
                              termination_reason=reason, resource_reason=None if timed_out else "total_tokens",
                              evidence=evidence)
        try:
            self._persist("worker_partial" if timed_out else "worker_wrapup", job.task_id, {
                "result": result.to_dict(), "source_snapshot": job.source_snapshot, "changed_during_work": False,
            }, background=True)
        except Exception:
            logger.exception("Could not persist Worker partial result")
        job.partial = result
        self._notify_result(job, result, partial=True)
        self.condition.notify_all()

    def _snapshot(self) -> str:
        excluded = [Path(self.coordinator.log_dir).resolve()]
        if self.durable is not None:
            excluded.append(self.durable.store.base)
        return repository_content_fingerprint(self.coordinator.repo_path, excluded_paths=tuple(excluded))

    def _run(self, job: WorkerJob) -> WorkerResult:
        cancel = _CombinedCancel(self.cancel, job)
        if cancel.is_set():
            return self._stopped_result(job)
        with self.condition:
            job.progress = {"phase": "started"}
        self.log.log_trace(EventType.WORKER_STARTED, 0, worker_id=job.task_id, worker_role=job.request.role)
        snapshot = self._snapshot()
        if job.source_snapshot is None:
            self._persist("worker_snapshot", job.task_id, {"task_id": job.task_id, "snapshot": snapshot},
                          background=True)
            job.source_snapshot = snapshot

        def progress(event):
            job.activity.record(event)
            if event.event_type is EventType.WORKER_WRAPUP_STARTED:
                with self.condition:
                    if job.partial is None and not cancel.is_set():
                        self._publish_partial(job, timed_out=False)
            if event.event_type is EventType.ACTION:
                action = event.payload["action"]
                with self.condition:
                    job.progress = {"step": event.payload["step"], "action": action["action_type"],
                                    "tool": (action.get("tool_call") or {}).get("name")}

        result = self.coordinator._run_worker(
            job.task_id, job.agent_id, job.request, job.token_limit, cancel, on_event=progress,
        )
        job.changed_during_work = snapshot != job.source_snapshot or job.source_snapshot != self._snapshot()
        if result.status == "canceled" and job.is_set() and not self.cancel.is_set():
            result = dataclasses.replace(result, status="timed_out", summary="Worker deadline elapsed.",
                                         termination_reason="timeout")
        return result

    def _stopped_result(self, job: WorkerJob, error: BaseException | None = None) -> WorkerResult:
        status = "failed" if error else "canceled" if self.cancel.is_set() else "timed_out"
        summary = f"Worker failed: {type(error).__name__}: {error}" if error else f"Worker {status} before running."
        usage, evidence = job.activity.snapshot()
        return WorkerResult(job.task_id, job.agent_id, job.request.role, status, summary, [], [], usage,
                            0.0, None, self.coordinator.parent_task_id, error is None,
                            token_limit=job.token_limit, termination_reason="worker_error" if error else
                            "timeout" if status == "timed_out" else "canceled", evidence=evidence)

    def _settle(self, job: WorkerJob) -> None:
        result = job.result
        self.coordinator.record_worker_result(result)
        self.coordinator.remaining_tokens += job.token_limit - self.coordinator.result_charge(result)

    def _complete(self, job: WorkerJob, future: Future) -> None:
        job.timer.cancel()
        if future.cancelled() and self.preserve:
            return
        try:
            result = self._stopped_result(job) if future.cancelled() else future.result()
        except BaseException as error:  # A Worker thread cannot interrupt its Lead.
            result = self._stopped_result(job, error)
        try:
            self._persist("worker_result", job.task_id, {
                "result": result.to_dict(), "source_snapshot": job.source_snapshot,
                "changed_during_work": job.changed_during_work,
            }, background=True)
        except Exception as error:
            logger.warning("Could not persist Worker result (%s)", type(error).__name__)
            result = dataclasses.replace(result, status="failed", summary="Worker result could not be persisted.")
        with self.condition:
            try:
                job.result = result
                self._settle(job)
            finally:
                self.condition.notify_all()
        self._notify_result(job, result)

    def ready_notices(self, delivered: set[str]) -> list[dict]:
        if not self.condition.acquire(blocking=False):
            return []
        try:
            ready = []
            for job in self.jobs.values():
                result = job.result or job.partial
                if result is not None:
                    receipt_id = (job.task_id + ":wrapup" if result.termination_reason == "budget_wrapup"
                                  else job.task_id)
                    if job.task_id in delivered or receipt_id in delivered:
                        continue
                    if (job.task_id + ":wrapup" in delivered and result.termination_reason == "resource_exhausted"
                            and not result.findings):
                        continue
                    ready.append({**result.notice(), "receipt_id": receipt_id})
                    if len(ready) == 3:
                        break
            return ready
        finally:
            self.condition.release()

    def _get(self, task_id: str) -> WorkerJob:
        if not isinstance(task_id, str) or task_id not in self.jobs:
            raise ValueError("unknown Worker task ID")
        return self.jobs[task_id]

    def status(self, task_id: str | None) -> list[dict]:
        with self.condition:
            jobs = [self._get(task_id)] if task_id is not None else self.jobs.values()
            return [job.status() for job in jobs]

    def wait(self, task_id: str, timeout: float | None) -> dict:
        if timeout is not None and (
            type(timeout) not in (int, float) or timeout < 0 or isinstance(timeout, float) and not math.isfinite(timeout)
        ):
            raise ValueError("timeout_seconds must be a finite non-negative number")
        duration = self.coordinator.timeout_seconds if timeout is None else min(timeout, self.coordinator.timeout_seconds)
        with self.condition:
            job = self._get(task_id)
            deadline = time.monotonic() + duration
            while (job.result is None and (job.partial is None or job.partial.status != "timed_out")
                   and not self.cancel.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(remaining, 0.1))
            status = job.status()
            result = job.result or job.partial
        if result is not None:
            status["result"] = result.to_dict()
            status["workspace_changed"] = bool(job.source_snapshot and
                                               (job.changed_during_work or job.source_snapshot != self._snapshot()))
        return status

    def close(self, *, preserve: bool = False) -> None:
        with self.condition:
            if self.closed:
                return
            self.closed = True
            self.preserve = preserve and self.durable is not None
            if not self.preserve:
                self.stop.set()
            self.condition.notify_all()
        self.pool.shutdown(wait=True, cancel_futures=True)


class WorkerStatusTool(BaseTool):
    name = "worker_status"
    effect = ToolEffect.READ_ONLY
    description = "Show background Worker states and recent actions immediately. Omit task_id to list all Workers."
    parameters_schema = {"type": "object", "properties": {"task_id": {"type": "string"}}}

    def __init__(self, background: BackgroundWorkers) -> None:
        self.background = background

    def execute(self, params: dict) -> ToolResult:
        try:
            return ToolResult(True, json.dumps(self.background.status(params.get("task_id")), ensure_ascii=False))
        except ValueError as error:
            return ToolResult(False, "", str(error), ToolErrorType.INVALID_ARGUMENTS)


class WorkerWaitTool(WorkerStatusTool):
    name = "worker_wait"
    description = (
        "Read a background Worker result or wait for a dependency. Use timeout_seconds=0 to fetch a ready result "
        "immediately. If still running at the deadline, returns current status. Check workspace_changed before using findings."
    )
    parameters_schema = {"type": "object", "required": ["task_id"], "properties": {
        "task_id": {"type": "string"}, "timeout_seconds": {"type": "number", "minimum": 0},
    }}
    metadata = {"worker_pool_tool": "wait"}

    def execute(self, params: dict) -> ToolResult:
        try:
            result = self.background.wait(params["task_id"], params.get("timeout_seconds"))
            return ToolResult(True, json.dumps(result, ensure_ascii=False))
        except (KeyError, ValueError) as error:
            return ToolResult(False, "", str(error), ToolErrorType.INVALID_ARGUMENTS)
