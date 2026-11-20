"""Bounded, read-only Supervisor workers around the existing synchronous Agent loop."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent.core import AgentConfig
from agent.runner import ExecutionRunner, RunRequest
from agent.task import RunStatus, Task
from harness.permission import ExecutionConstraints
from llm.usage import SessionUsage
from task.engine import TaskEngine
from tools.base import BaseTool, ToolEffect, ToolRegistry, ToolResult


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

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class _CombinedCancel:
    def __init__(self, parent, local: threading.Event) -> None:
        self.parent = parent
        self.local = local

    def is_set(self) -> bool:
        parent_is_set = getattr(self.parent, "is_set", None)
        return self.local.is_set() or (bool(parent_is_set()) if callable(parent_is_set) else bool(self.parent))


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
    ) -> None:
        if min(max_workers, max_tasks, total_tokens) < 1 or timeout_seconds <= 0:
            raise ValueError("worker limits must be positive")
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
        self.remaining_tokens = total_tokens
        self.timeout_seconds = timeout_seconds
        self.sandbox_workers = sandbox_workers
        self.usage = SessionUsage()
        self.usage_complete = True
        self.delegation_count = 0
        self.worker_count = 0
        self.worker_failure_count = 0
        self.worker_tool_calls = 0
        self.closed = False

    def delegate(self, requests: list[WorkerRequest], *, operation_id: str | None = None) -> list[WorkerResult]:
        if self.closed:
            raise RuntimeError("worker coordinator is closed after cancellation or timeout")
        if not 1 <= len(requests) <= self.max_tasks:
            raise ValueError(f"delegate between 1 and {self.max_tasks} tasks")
        if any(not isinstance(request.role, str) or request.role not in PROFILES
               or not isinstance(request.task, str) or not request.task.strip() for request in requests):
            raise ValueError(f"each worker needs a non-empty task and one of these roles: {', '.join(PROFILES)}. "
                             "Retry with the same task and a listed role; put custom role instructions in task.")
        if self.remaining_tokens < len(requests):
            raise RuntimeError("worker token budget exhausted")
        self.delegation_count += 1
        local_cancel = threading.Event()
        cancel = _CombinedCancel(self.cancel_event, local_cancel)
        allocation = min(self.remaining_tokens // len(requests), max(p.max_tokens for p in PROFILES.values()))
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
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(records)), thread_name_prefix="forge-worker")
        started = time.monotonic()
        futures = {pool.submit(self._run_worker, task_id, agent_id, request, min(allocation, PROFILES[request.role].max_tokens), cancel):
                   (task_id, agent_id, request) for task_id, agent_id, request in records}
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
        for future, (task_id, agent_id, request) in futures.items():
            if future in done:
                try:
                    result = future.result()
                except Exception as exc:  # worker failure must not discard siblings
                    result = WorkerResult(task_id, agent_id, request.role, "failed", f"{type(exc).__name__}: {exc}",
                                          [], [], {}, time.monotonic() - started, None, self.parent_task_id, False)
            else:
                future.cancel()
                result = WorkerResult(task_id, agent_id, request.role,
                                      "canceled" if canceled else "timed_out",
                                      "Worker canceled or timed out; an in-flight synchronous provider call may still finish.",
                                      [], [], {}, time.monotonic() - started, None, self.parent_task_id, False)
            results.append(result)
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
            from task.engine import STATUS_COMPLETED, STATUS_FAILED

            if self.engine.get_task(task_id).status not in {STATUS_COMPLETED, STATUS_FAILED}:
                self.engine.complete_task(task_id) if result.status == "success" else self.engine.fail_task(task_id)
            self.usage.add(SessionUsage.from_dict(result.token_usage))
            if result.usage_complete is False:
                self.usage_complete = False
        self.remaining_tokens = max(0, self.remaining_tokens - sum(r.token_usage.get("total_tokens", 0) for r in results))
        pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _run_worker(self, task_id: str, agent_id: str, request: WorkerRequest, token_limit: int, cancel) -> WorkerResult:
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
                multi_agent_enabled=False, planning_mode="off",
                max_steps=profile.max_steps, max_model_calls=profile.max_steps,
                max_total_tokens=token_limit, cancel_event=cancel,
                budget_tokens=budget_tokens,
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
                    raw = saved["result"]
                    from agent.task import RunResult, RunStatus

                    result = RunResult(task_id, RunStatus(raw["status"]), raw["summary"], raw["steps_taken"],
                                       usage=SessionUsage.from_dict(raw["usage"]), trace_path=raw["trace_path"],
                                       usage_complete=raw["usage_complete"])
                else:
                    result = runner.run(RunRequest(
                        task, execution_constraints=ExecutionConstraints(read_only=True), entrypoint="worker",
                        run_id=task_id, parent_run_id=self.parent_run_id, resume=saved is not None,
                        parent_owner=self.parent_owner if not saved or saved["scope_id"] != task_id else None,
                    ))
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
                            result.usage_complete, memory_candidates)

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
                if worker.get("usage_complete") is False:
                    self.usage_complete = False
        self.remaining_tokens = max(0, self.remaining_tokens - self.usage.total_tokens)


class DelegateTasksTool(BaseTool):
    def __init__(self, coordinator: AgentCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "delegate_tasks"

    @property
    def description(self) -> str:
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
            results = (self.coordinator.delegate(requests, operation_id=operation_id)
                       if operation_id else self.coordinator.delegate(requests))
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            return ToolResult(False, "", error=str(exc))
        return ToolResult(True, json.dumps([r.to_dict() for r in results], ensure_ascii=False))
