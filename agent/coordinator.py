"""Bounded, read-only Supervisor workers around the existing synchronous Agent loop."""

from __future__ import annotations

import dataclasses
import json
import math
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent.core import AgentConfig
from agent.runner import ExecutionRunner, RunRequest
from agent.task import EventType, RunResult, Task
from harness.permission import ExecutionConstraints
from llm.usage import SessionUsage, TokenUsage
from task.engine import TaskEngine
from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolRegistry, ToolResult


@dataclass(frozen=True)
class AgentProfile:
    name: str
    instruction: str
    tools: frozenset[str]
    max_steps: int
    max_tokens: int


PROFILES = {
    "explorer": AgentProfile(
        "explorer", "Explore the repository structure and call paths. Return concise JSON with summary, findings, recommendations and optional memory_candidates for Lead review. Do not change files.",
        frozenset({"file_read", "file_view", "search_text", "find_files", "find_symbol", "git_status", "memory_search", "memory_read"}), 12, 24_000,
    ),
    "test_analyst": AgentProfile(
        "test_analyst", "Inspect existing tests and likely regressions. Return concise JSON with summary, findings, recommendations and optional memory_candidates for Lead review. Do not run tests or change files.",
        frozenset({"file_read", "file_view", "search_text", "find_files", "find_symbol", "git_status", "memory_search", "memory_read"}), 12, 24_000,
    ),
    "reviewer": AgentProfile(
        "reviewer", "Review the current diff for bugs and safety risks. Return concise JSON with summary, findings, recommendations and optional memory_candidates for Lead review, including file and line evidence. Do not change files.",
        frozenset({"file_read", "file_view", "search_text", "find_files", "find_symbol", "git_status", "git_diff", "memory_search", "memory_read"}), 12, 24_000,
    ),
}


@dataclass(frozen=True)
class WorkerRequest:
    role: str
    task: str


@dataclass
class WorkerResult:
    task_id: str
    agent_id: str
    role: str
    status: str
    summary: str
    findings: list[dict]
    recommendations: list[str]
    token_usage: dict
    elapsed_time: float
    trace_reference: str | None
    parent_task_id: str
    usage_complete: bool | None = None
    memory_candidates: list[dict] | None = None
    token_limit: int | None = None
    termination_reason: str | None = None
    resource_reason: str | None = None
    evidence: list[dict] | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def notice(self) -> dict:
        return {
            "task_id": self.task_id, "role": self.role, "status": self.status,
            "summary": self.summary[:480],
            "findings": [json.dumps(item, ensure_ascii=False)[:320] for item in self.findings[:2]],
            "evidence": (self.evidence or [])[-2:],
            "termination_reason": self.termination_reason, "resource_reason": self.resource_reason,
            "trace_reference": self.trace_reference,
        }


class WorkerProgress:
    """Keep a bounded copy of confirmed tool observations for interruption."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.usage = SessionUsage()
        self.evidence = deque(maxlen=3)
        self.action = None

    def record(self, event) -> None:
        with self.lock:
            payload = event.payload
            if event.event_type is EventType.LLM_CALL_FINISHED:
                raw = payload.get("usage", {})
                self.usage.record(TokenUsage(**{key: raw[key] for key in TokenUsage.__dataclass_fields__ if key in raw}))
            elif event.event_type is EventType.ACTION:
                self.action = payload["action"].get("tool_call")
            elif event.event_type is EventType.OBSERVATION:
                self._observe(payload, event.event_id)

    def _observe(self, payload: dict, event_id: str) -> None:
        observation = payload["observation"]
        if observation["status"] != "success" or not observation["output"].strip():
            return
        parameters = (self.action or {}).get("params", {})
        location = {key: str(parameters[key])[:160] for key in ("path", "pattern", "query", "start_line", "end_line")
                    if key in parameters}
        self.evidence.append({"step": payload["step"], "tool": observation["tool_name"], "location": location,
                              "excerpt": observation["output"][:240], "event_id": event_id})

    def snapshot(self) -> tuple[dict, list[dict]]:
        with self.lock:
            return self.usage.to_dict(), list(self.evidence)

    @staticmethod
    def saved_evidence(trace_path: str | None) -> list[dict]:
        progress = WorkerProgress()
        if trace_path:
            try:
                with open(trace_path, encoding="utf-8") as trace:
                    for line in trace:
                        row = json.loads(line)
                        if row["event_type"] == EventType.ACTION.value:
                            progress.action = row["payload"]["action"].get("tool_call")
                        elif row["event_type"] == EventType.OBSERVATION.value:
                            progress._observe(row["payload"], row["event_id"])
            except (OSError, ValueError, KeyError):
                pass
        return list(progress.evidence)


class _CombinedCancel:
    def __init__(self, parent, local: threading.Event) -> None:
        self.parent = parent
        self.local = local

    def is_set(self) -> bool:
        parent_is_set = getattr(self.parent, "is_set", None)
        return self.local.is_set() or (bool(parent_is_set()) if callable(parent_is_set) else bool(self.parent))

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True


class AgentCoordinator:
    """Own worker dispatch and accounting; Agent.run remains the execution loop."""

    def __init__(
        self, *, repo_path: str, log_dir: str, parent_task_id: str,
        backend_factory: Callable[[str], object], registry_factory: Callable,
        agent_config: AgentConfig, engine: TaskEngine,
        cancel_event=None, max_workers: int = 3, max_tasks: int = 3,
        total_tokens: int = 72_000, timeout_seconds: float = 120.0,
        sandbox_workers: bool = False,
        parent_run_id: str | None = None,
        parent_owner=None,
        durable=None,
    ) -> None:
        if min(max_workers, max_tasks) < 1 or total_tokens < 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("worker concurrency and timeout must be positive; token allowance cannot be negative")
        self.repo_path = str(Path(repo_path).resolve())
        self.log_dir = log_dir
        self.parent_task_id = parent_task_id
        self.backend_factory = backend_factory
        self.registry_factory = registry_factory
        self.agent_config = agent_config
        self.parent_run_id = parent_run_id
        self.parent_owner = parent_owner
        self.engine = engine
        self.cancel_event = cancel_event
        self.max_workers = max_workers
        self.max_tasks = max_tasks
        self.run_token_limit = agent_config.max_total_tokens
        self.pool_tokens = min(total_tokens, self.run_token_limit // 3) if self.run_token_limit is not None else total_tokens
        self.remaining_tokens = self.pool_tokens
        self.lead_used = 0
        self.lead_next_request = 0
        self.timeout_seconds = timeout_seconds
        self.sandbox_workers = sandbox_workers
        self.usage = SessionUsage()
        self.usage_complete = True
        self.delegation_count = 0
        self.worker_count = 0
        self.worker_failure_count = 0
        self.worker_tool_calls = 0
        self.closed = False
        self.background = None
        if agent_config.worker_mode == "background":
            from agent.background_workers import BackgroundWorkers

            self.background = BackgroundWorkers(self, durable)

    def lead_token_limit(self) -> int | None:
        if self.run_token_limit is None:
            return None
        return self.run_token_limit - (self.pool_tokens - self.remaining_tokens)

    def available_tokens(self) -> int:
        limit = self.lead_token_limit()
        remaining = self.remaining_tokens
        if limit is not None:
            remaining = min(remaining, limit - self.lead_used - self.lead_next_request)
        return max(0, remaining)

    def prompt(self) -> str:
        profiles = ", ".join(f"{role}: up to {profile.max_tokens:,}" for role, profile in PROFILES.items())
        return (
            f"[Worker delegation] Available Worker pool: {self.available_tokens():,} cumulative tokens. "
            f"Role caps: {profiles}. Delegate only independent work that will inform a Lead decision. "
            "Describe one question, known context, evidence to return, and a clear stopping point per Worker. "
            "Handle quick lookups yourself. Continue independent work; only wait for a required result."
        )

    def allocations(self, requests: list[WorkerRequest]) -> list[int]:
        self.validate_requests(requests)
        each = self.available_tokens() // len(requests)
        return [min(each, PROFILES[request.role].max_tokens) for request in requests]

    def validate_requests(self, requests: list[WorkerRequest]) -> None:
        if self.closed:
            raise RuntimeError("worker coordinator is closed after cancellation or timeout")
        if not 1 <= len(requests) <= self.max_tasks:
            raise ValueError(f"delegate between 1 and {self.max_tasks} tasks")
        if any(not isinstance(request.role, str) or request.role not in PROFILES
               or not isinstance(request.task, str) or not request.task.strip() for request in requests):
            raise ValueError(f"each worker needs a non-empty task and one of these roles: {', '.join(PROFILES)}. "
                             "Retry with the same task and a listed role; put custom role instructions in task.")
        if self.available_tokens() < len(requests):
            raise RuntimeError("worker token budget exhausted")

    def worker_records(self, requests: list[WorkerRequest], operation_id: str | None):
        records = []
        for index, request in enumerate(requests):
            identity = uuid.uuid5(uuid.NAMESPACE_OID, f"{operation_id}:{index}") if operation_id else uuid.uuid4()
            agent_id = f"worker_{identity.hex[:12]}"
            task_id = f"task_{identity.hex}"
            from task.engine import TaskNotFound

            try:
                existing = self.engine.get_task(task_id)
            except TaskNotFound:
                self.engine.create_task(request.task[:80], request.task, parent_id=self.parent_task_id,
                                        agent_id=agent_id, role=request.role, task_id=task_id)
            else:
                if (existing.parent_id, existing.agent_id, existing.role, existing.description) != (
                    self.parent_task_id, agent_id, request.role, request.task,
                ):
                    raise ValueError("saved worker task identity differs")
            records.append((task_id, agent_id, request))
        return records

    def delegate(self, requests: list[WorkerRequest], *, operation_id: str | None = None) -> list[WorkerResult]:
        allocations = self.allocations(requests)
        self.delegation_count += 1
        local_cancel = threading.Event()
        cancel = _CombinedCancel(self.cancel_event, local_cancel)
        records = self.worker_records(requests, operation_id)
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(records)), thread_name_prefix="forge-worker")
        started = time.monotonic()
        futures = {}
        for (task_id, agent_id, request), limit in zip(records, allocations, strict=True):
            progress = WorkerProgress()
            future = pool.submit(self._run_worker, task_id, agent_id, request, limit, cancel,
                                 on_event=progress.record)
            futures[future] = task_id, agent_id, request, limit, progress
        done, pending = set(), set(futures)
        deadline = started + self.timeout_seconds
        while pending and not cancel.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            completed, pending = wait(pending, timeout=min(0.1, remaining), return_when=FIRST_COMPLETED)
            done.update(completed)
        canceled = cancel.is_set()
        if pending or canceled:
            local_cancel.set()
            self.closed = True
        results: list[WorkerResult] = []
        for future, (task_id, agent_id, request, limit, progress) in futures.items():
            if future in done or future.done():
                try:
                    result = future.result()
                except Exception as exc:  # worker failure must not discard siblings
                    usage, evidence = progress.snapshot()
                    result = WorkerResult(task_id, agent_id, request.role, "failed", f"{type(exc).__name__}: {exc}",
                                          [], [], usage, time.monotonic() - started, None, self.parent_task_id, False,
                                          token_limit=limit, termination_reason="worker_error", evidence=evidence)
            else:
                future.cancel()
                usage, evidence = progress.snapshot()
                result = WorkerResult(task_id, agent_id, request.role,
                                      "canceled" if canceled else "timed_out",
                                      "Worker stopped; saved observations describe progress.",
                                      [], [], usage, time.monotonic() - started, None, self.parent_task_id, False,
                                      token_limit=limit, termination_reason="canceled" if canceled else "timeout",
                                      evidence=evidence)
            results.append(result)
            self.record_worker_result(result)
        self.remaining_tokens -= sum(self.result_charge(result) for result in results)
        pool.shutdown(wait=False, cancel_futures=True)
        return results

    @staticmethod
    def result_charge(result: WorkerResult) -> int:
        used = result.token_usage.get("total_tokens", 0)
        return max(used, result.token_limit or 0) if result.usage_complete is False else used

    def record_worker_result(self, result: WorkerResult) -> None:
        from task.engine import STATUS_COMPLETED, STATUS_FAILED

        self.worker_count += 1
        self.worker_failure_count += int(result.status != "success")
        if result.trace_reference:
            try:
                with open(result.trace_reference, encoding="utf-8") as trace:
                    self.worker_tool_calls += sum(
                        json.loads(line).get("event_type") == "tool_execution_started"
                        for line in trace
                    )
            except (OSError, ValueError):
                self.usage_complete = False
        if self.engine.get_task(result.task_id).status not in {STATUS_COMPLETED, STATUS_FAILED}:
            self.engine.complete_task(result.task_id) if result.status == "success" else self.engine.fail_task(result.task_id)
        self.usage.add(SessionUsage.from_dict(result.token_usage))
        if result.usage_complete is False:
            self.usage_complete = False

    def register_tools(self, registry: ToolRegistry) -> None:
        tools = [DelegateTasksTool(self)]
        if self.background is not None:
            from agent.background_workers import WorkerStatusTool, WorkerWaitTool

            tools.extend([WorkerStatusTool(self.background), WorkerWaitTool(self.background)])
        for tool in tools:
            existing = registry.get_tool(tool.name)
            if existing is not None and type(existing) is not type(tool):
                raise ValueError(f"{tool.name} is already registered by another tool")
            registry.register(tool, replace=True)

    def _run_worker(self, task_id: str, agent_id: str, request: WorkerRequest, token_limit: int, cancel,
                    *, on_event=None) -> WorkerResult:
        started = time.monotonic()
        from task.engine import STATUS_PENDING

        if self.engine.get_task(task_id).status == STATUS_PENDING:
            self.engine.claim_task(task_id, owner=request.role)
        profile = PROFILES[request.role]
        created = self.backend_factory(request.role)
        backend, budget_tokens = created if isinstance(created, tuple) else (created, self.agent_config.budget_tokens)
        runtime = None
        try:
            if self.sandbox_workers:
                from tools.runtime import CONTAINER_WORKDIR, DockerRuntime
                runtime = DockerRuntime(repo_path=self.repo_path, readonly_root=True, network=False)
                preflight = runtime.preflight(cwd=self.repo_path)
                if not preflight.success:
                    raise RuntimeError(f"worker Docker preflight failed: {preflight.output}")
            registry = self.registry_factory(self.agent_config, None, runtime,
                                             default_cwd=self.repo_path, workspace=self.repo_path)
            worker_tools = ToolRegistry()
            for name in sorted(profile.tools):
                tool = registry.get_tool(name)
                if tool is not None:
                    worker_tools.register(tool)
            if self.agent_config.memory_config is not None and self.agent_config.memory_config.enabled:
                from project_memory.runtime import MemoryRuntime
                from project_memory.tools import MemoryReadTool, MemorySearchTool
                settings = self.agent_config.memory_config
                memory = MemoryRuntime(self.repo_path, root=settings.root,
                                       budget=settings.context_budget_tokens, max_results=settings.max_results)
                worker_tools.register(MemorySearchTool(memory)).register(MemoryReadTool(memory))
            cfg = dataclasses.replace(
                self.agent_config, run_profile="safe", finish_policy="advisory",
                multi_agent_enabled=False, worker_mode="sync", planning_mode="off",
                max_steps=profile.max_steps, max_model_calls=profile.max_steps,
                max_total_tokens=token_limit, cancel_event=cancel,
                worker_budget=True, worker_coordinator=None, llm_max_retries=1,
                budget_tokens=budget_tokens,
                repo_map_budget=min(self.agent_config.repo_map_budget or 1_200, 1_200),
                prepare_next_turn=None, skills_enabled=False,
                execution_workspace=CONTAINER_WORKDIR if self.sandbox_workers else None,
                repo_map_cache_dir=str(Path(self.log_dir) / "worker-cache" / task_id),
                memory_config=(dataclasses.replace(self.agent_config.memory_config, auto_capture=False)
                               if self.agent_config.memory_config is not None else None),
            )
            runner = ExecutionRunner(backend=backend, registry=worker_tools, config=cfg,
                                     log_dir=self.log_dir, mcp_config=None)
            task = Task(
                description=f"{profile.instruction}\n\nAssigned task: {request.task}",
                repo_path=self.repo_path, task_id=task_id, parent_task_id=self.parent_task_id,
                agent_id=agent_id, role=request.role, max_steps=profile.max_steps,
            )
            try:
                saved = self._worker_checkpoint(task_id)
                if saved and saved.get("result") is not None:
                    result = RunResult.from_dict(saved["result"])
                else:
                    result = runner.run(RunRequest(
                        task, execution_constraints=ExecutionConstraints(read_only=True), entrypoint="worker",
                        run_id=task_id, parent_run_id=self.parent_run_id, resume=saved is not None,
                        parent_owner=self.parent_owner if not saved or saved["scope_id"] != task_id else None,
                    ), on_event=on_event)
            finally:
                runner.close()
        finally:
            if runtime is not None:
                runtime.cleanup()
        summary, findings, recommendations, memory_candidates = result.summary, [], [], []
        try:
            parsed = json.loads(result.summary)
            if isinstance(parsed, dict):
                summary = str(parsed.get("summary", summary))
                findings = parsed.get("findings", []) if isinstance(parsed.get("findings", []), list) else []
                recommendations = parsed.get("recommendations", []) if isinstance(parsed.get("recommendations", []), list) else []
                raw_candidates = parsed.get("memory_candidates", [])
                memory_candidates = ([item for item in raw_candidates if isinstance(item, dict)][:5]
                                     if isinstance(raw_candidates, list) else [])
        except (ValueError, TypeError):
            pass
        return WorkerResult(task_id, agent_id, request.role, result.status.value, summary,
                            findings, recommendations, result.usage.to_dict(),
                            time.monotonic() - started, result.trace_path, self.parent_task_id,
                            result.usage_complete, memory_candidates,
                            token_limit, result.termination_reason, result.resource_reason,
                            WorkerProgress.saved_evidence(result.trace_path) if not result.is_success() else None)

    def _worker_checkpoint(self, task_id: str) -> dict | None:
        settings = self.agent_config.durable_runtime_config
        if not settings or not settings.enabled:
            return None
        from durable_runtime.run_manager import checked_payload
        from durable_runtime.store import DurableRuntimeStore, StoreFormatError
        from project_memory.identity import ProjectIdentityResolver

        with DurableRuntimeStore(ProjectIdentityResolver.resolve(self.repo_path), settings.root,
                                 timeout_ms=settings.busy_timeout_ms) as store:
            row = store._conn.execute(
                """SELECT r.parent_run_id, c.payload_json, c.payload_digest
                   FROM runs r JOIN run_checkpoints c ON c.checkpoint_id=r.checkpoint_id AND c.run_id=r.run_id
                   WHERE r.run_id=? AND r.scope_id IN (?, ?) AND r.entrypoint='worker'""",
                (task_id, task_id, self.parent_owner.scope_id if self.parent_owner else task_id),
            ).fetchone()
            if row is None:
                return None
            if row["parent_run_id"] != self.parent_run_id:
                raise StoreFormatError("saved worker belongs to a different parent Run")
            return checked_payload(row["payload_json"], row["payload_digest"])

    def restore_delegation_usage(self, run) -> None:
        from durable_runtime.run_manager import digest, encode
        from durable_runtime.store import StoreFormatError

        rows = run.store._conn.execute(
            """SELECT result_json, observation_json, result_digest FROM operations
               WHERE run_id=? AND replay_policy='worker_resume' AND state='result_committed'""", (run.run_id,),
        )
        charged = 0
        for payload, observation, checksum in rows:
            if digest(encode([payload, observation])) != checksum:
                raise StoreFormatError("saved delegation result digest differs")
            tool_result = json.loads(payload)
            if not tool_result["success"]:
                continue
            self.delegation_count += 1
            for worker in json.loads(tool_result["output"]):
                self.worker_count += 1
                self.worker_failure_count += int(worker["status"] != "success")
                self.usage.add(SessionUsage.from_dict(worker["token_usage"]))
                charged += self.result_charge(WorkerResult(**worker))
                if worker.get("usage_complete") is False:
                    self.usage_complete = False
        self.remaining_tokens -= charged


class DelegateTasksTool(BaseTool):
    def __init__(self, coordinator: AgentCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "delegate_tasks"

    @property
    def description(self) -> str:
        if self.coordinator.background is not None:
            return (
                "Start independent read-only workers and immediately return their task IDs. "
                f"Submit at most {self.coordinator.max_tasks} tasks per call. "
                "Continue independent work, use worker_status for progress and worker_wait to collect needed results. "
                "Finishing the parent cancels outstanding work."
            )
        return "Delegate independent read-only repository analysis tasks to bounded parallel workers."

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect.READ_ONLY

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {"tasks": {"type": "array", "items": {
            "type": "object", "properties": {
                "role": {"type": "string", "enum": list(PROFILES)},
                "task": {"type": "string", "description": "Non-empty analysis task; use this for custom instructions."},
            },
            "required": ["role", "task"]}}}, "required": ["tasks"]}

    def execute(self, params: dict, *, operation_id: str | None = None) -> ToolResult:
        try:
            raw = params["tasks"]
            if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                raise ValueError("tasks must be an array of objects")
            requests = [WorkerRequest(item["role"], item["task"]) for item in raw]
            if self.coordinator.background is not None:
                results = self.coordinator.background.submit(requests, operation_id=operation_id)
            else:
                results = [r.to_dict() for r in self.coordinator.delegate(requests, operation_id=operation_id)]
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, "", error=str(exc), error_type=ToolErrorType.INVALID_ARGUMENTS)
        except RuntimeError as exc:
            return ToolResult(False, "", error=str(exc))
        return ToolResult(True, json.dumps(results, ensure_ascii=False))
