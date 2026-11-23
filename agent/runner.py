"""Shared execution composition root for the CLI and evaluation harness."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from agent.core import Agent, AgentConfig, PrepareNextTurn
from agent.event_log import EventLog
from agent.orchestrate import orchestrate_run
from agent.task import EventType, RunResult, RunStatus, Task
from agent.trace_v2 import bind_trace_context
from context.history import ConversationHistory
from context.token_budget import TokenBudget
from config.schema import MCPConfig
from harness import ExecutionConstraints, Hooks, PermissionManager, ToolExecutor
from llm.usage import SessionUsage
from mcp_integration.registry import attach_mcp_tools
from runtime.worktree import WorktreeResultPolicy
from task.engine import TaskEngine, TaskNotFound
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def _merge_worker_usage(result: RunResult, coordinator, log: EventLog) -> None:
    if not coordinator.delegation_count:
        return
    usage = coordinator.usage
    result.worker_usage = usage.snapshot()
    result.usage.add(usage)
    result.total_tokens = result.usage.total_tokens
    if not coordinator.usage_complete:
        result.usage_complete = False
    log.log_trace(EventType.WORKERS_SUMMARY, result.steps_taken,
                  parent_task_id=result.task_id, worker_usage=usage.to_dict(),
                  usage_complete=coordinator.usage_complete,
                  delegation_count=coordinator.delegation_count,
                  worker_count=coordinator.worker_count,
                  worker_success_count=coordinator.worker_count - coordinator.worker_failure_count,
                  worker_failure_count=coordinator.worker_failure_count,
                  worker_tool_calls=coordinator.worker_tool_calls)


def _merge_context_policy_usage(result: RunResult, callback) -> None:
    """Include semantic-compaction side calls in the run's provider usage totals."""
    consume = getattr(callback, "consume_usage", None)
    if not callable(consume):
        return
    side_usage = consume()
    if side_usage is None or (
        not side_usage.llm_calls and not side_usage.total_tokens
    ):
        return
    if result.usage is None:
        result.usage = SessionUsage()
    result.usage.add(side_usage)
    result.total_tokens = result.usage.total_tokens


@dataclass(frozen=True)
class AcceptanceContract:
    require_changes: bool = False
    require_tests: bool = False
    required_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    verifier: Callable[[Path], bool] | None = None  # Runner 持有，不写入 Agent History

    @classmethod
    def from_task(cls, task: Task) -> "AcceptanceContract":
        return cls(task.require_changes, task.require_tests)

    def apply(self, task: Task) -> Task:
        return dataclasses.replace(
            task,
            require_changes=self.require_changes,
            require_tests=self.require_tests,
        )

    def has_independent_checks(self) -> bool:
        return bool(self.required_paths or self.forbidden_paths or self.verifier)


@dataclass
class RunRequest:
    task: Task
    history: ConversationHistory | None = None
    isolate: bool = False
    sandbox: bool = False
    hooks: Hooks | None = None
    permission: PermissionManager | None = None
    cancel_event: object | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    result_policy: WorktreeResultPolicy | str = WorktreeResultPolicy.KEEP_IF_CHANGED
    acceptance: AcceptanceContract | None = None
    session_id: str | None = None
    # Product entrypoints may set this explicitly. None keeps current callers
    # compatible and resolves from the existing request shape.
    entrypoint: str | None = None
    execution_constraints: ExecutionConstraints = ExecutionConstraints()
    run_id: str | None = None
    resume: bool = False
    parent_owner: object | None = None
    parent_run_id: str | None = None


class ExecutionRunner:
    """Keep product-specific I/O outside the shared execution lifecycle."""

    def __init__(
        self,
        *,
        backend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
        log_dir: str = "./logs",
        registry_builder: Callable | None = None,
        engine: TaskEngine | None = None,
        confirm_callback=None,
        bus=None,
        mcp_config: MCPConfig | None = None,
        worker_backend_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.config = config or AgentConfig()
        self.log_dir = log_dir
        self.registry_builder = registry_builder
        self.engine = engine
        self.confirm_callback = confirm_callback
        self.bus = bus
        self.mcp_config = mcp_config
        self.worker_backend_factory = worker_backend_factory
        self._mcp_manager = None
        self._owned_worker_engine = None
        executor = ToolExecutor(
            registry,
            hooks=self.config.hooks,
            confirm_callback=confirm_callback,
        )
        self.agent = Agent(backend, registry, self.config, executor=executor)
        self._agent_key = None

    def run(
        self,
        request: RunRequest,
        *,
        log: EventLog | None = None,
        on_event=None,
    ) -> RunResult:
        settings = self.config.durable_runtime_config
        if settings is None or not settings.enabled:
            if request.resume:
                raise ValueError("resuming requires durable_runtime.enabled")
            return self._run(request, log=log, on_event=on_event)

        from durable_runtime.operation_journal import OutcomeUnknown
        from durable_runtime.run_manager import DurableRun, digest, encode
        from durable_runtime.store import DurableRuntimeStore
        from project_memory.identity import ProjectIdentityResolver
        from context.compaction import TraceableCompaction

        run_id = request.run_id or uuid.uuid4().hex
        identity = ProjectIdentityResolver.resolve(request.task.repo_path)
        acceptance = request.acceptance or AcceptanceContract.from_task(request.task)
        entrypoint = _resolve_entrypoint(request)
        task = acceptance.apply(request.task)
        contract = {"require_changes": acceptance.require_changes, "require_tests": acceptance.require_tests,
                    "required_paths": acceptance.required_paths, "forbidden_paths": acceptance.forbidden_paths,
                    "custom_verifier": acceptance.verifier is not None}
        cfg_fields = {
            key: value for key, value in vars(self.config).items()
            if key != "prepare_next_turn" and (key, value) != ("worker_mode", "sync")
            and (isinstance(value, (str, int, float, tuple, bool)) or value is None)
        }
        policy = request.prepare_next_turn if request.prepare_next_turn is not None else self.config.prepare_next_turn
        compaction = ({
            "threshold": policy.threshold, "target_ratio": policy.target_ratio,
            "keep_recent_tokens": policy.keep_recent_tokens, "max_summary_chars": policy.max_summary_chars,
            "pruner": type(policy.pruner).__qualname__, "summarizer": type(policy.semantic_summarizer).__qualname__,
        } if type(policy) is TraceableCompaction else None)
        config_digest = digest(encode({
            "provider": type(self.backend).__name__, "model": self.backend.model_name,
            "endpoint": str(getattr(getattr(self.backend, "_client", None), "base_url", "")),
            "max_output_tokens": getattr(self.backend, "_max_tokens", None),
            "agent": cfg_fields, "read_only": request.execution_constraints.read_only,
            "archive_enabled": settings.archive_enabled, "archive_read_max_chars": settings.archive_read_max_chars,
            "compaction": compaction,
            "memory": dataclasses.asdict(self.config.memory_config) if self.config.memory_config else None,
            "mcp": dataclasses.asdict(self.mcp_config) if self.mcp_config else None,
        }))
        dynamic_tools = {"memory_read", "memory_propose", "memory_search", "delegate_tasks", "worker_status",
                         "worker_wait", "archive_read"}
        registry_digest = digest(encode([dataclasses.asdict(schema) for schema in self.registry.get_schemas()
                                         if schema.name not in dynamic_tools]))
        resumable = (
            (entrypoint in {"cli", "worker"} and request.history is None
             or entrypoint == "chat" and request.history is not None and request.session_id)
            and (not request.sandbox or request.isolate)
            and request.permission is None
            and request.hooks is None and self.config.hooks is None
            and (policy is None or compaction is not None and settings.archive_enabled)
            and acceptance.verifier is None
            and not getattr(self.backend, "_budget", None)
        )
        request_snapshot = {"isolate": request.isolate, "sandbox": request.sandbox, "resumable": bool(resumable)}
        if request.isolate:
            request_snapshot["result_policy"] = WorktreeResultPolicy(request.result_policy).value
        with DurableRuntimeStore(identity, settings.root, timeout_ms=settings.busy_timeout_ms) as store:
            parent_owner = request.parent_owner
            if parent_owner is not None and (entrypoint != "worker" or not request.parent_run_id):
                raise ValueError("a borrowed Owner requires a supervised Worker Run")
            scope_id = parent_owner.scope_id if parent_owner is not None else request.session_id or run_id
            if parent_owner is not None and not store._conn.execute(
                "SELECT 1 FROM runs WHERE run_id=? AND scope_id=? AND entrypoint!='worker'",
                (request.parent_run_id, scope_id),
            ).fetchone():
                raise ValueError("Worker parent Run is missing from the shared scope")
            durable = DurableRun(store, run_id, scope_id, parent_owner=parent_owner)
            if settings.archive_enabled and parent_owner is None:
                from context.archive import ContextArchive

                durable.archive = ContextArchive(store, durable.owner.scope_id, settings.archive_inline_limit_bytes,
                                                 run_id=run_id, session_id=request.session_id)
            if request.resume:
                if not request.run_id or not resumable:
                    raise ValueError("run configuration does not support safe resumption")
                durable.resume(task, entrypoint, contract, config_digest, registry_digest, request_snapshot)
            else:
                durable.create(task, entrypoint, session_id=request.session_id, parent_run_id=request.parent_run_id,
                               request=request_snapshot,
                               acceptance=contract, config_digest=config_digest,
                               tool_registry_digest=registry_digest)
            try:
                saved = durable.load_checkpoint() if request.resume else {}
                if saved.get("agent_result") and ("acceptance_inputs" in saved or "acceptance_status" in saved):
                    result = _resume_independent_acceptance(
                        durable, acceptance, task, saved, entrypoint, request.session_id, on_event,
                    )
                else:
                    result = self._run(request, log=log, on_event=on_event, durable=durable)
                result.run_id = run_id
                if result.termination_reason == "paused":
                    store.pause_marker(run_id).unlink(missing_ok=True)
                    return result
                state = durable.load_checkpoint()
                state.pop("pending_model_call", None)
                if request.history is not None:
                    state["history"] = request.history.to_dicts()
                durable.checkpoint(result.steps_taken + 1, "terminal",
                                   {**state, "result": result.to_dict()},
                                   status=(result.status.value if result.status in {
                                       RunStatus.FAILED, RunStatus.CANCELED, RunStatus.INCOMPLETE,
                                   } else "completed"))
                return result
            except OutcomeUnknown:
                state = durable.load_checkpoint()
                durable.checkpoint(state["next_step"], "reconciliation_required", state,
                                   status="reconciliation_required")
                raise
            finally:
                # The direct Runner can reuse its Agent across Chat rounds.
                if self.agent._cfg.durable_run is durable:
                    self.agent._cfg = dataclasses.replace(self.agent._cfg, durable_run=None)

    def _run(
        self,
        request: RunRequest,
        *,
        log: EventLog | None = None,
        on_event=None,
        durable=None,
    ) -> RunResult:
        acceptance = request.acceptance or AcceptanceContract.from_task(request.task)
        task = acceptance.apply(request.task)
        entrypoint = _resolve_entrypoint(request)
        contract_paths, path_error = _normalize_contract_paths(
            acceptance.required_paths + acceptance.forbidden_paths
        )
        if durable and durable.restoring and not request.isolate and contract_paths:
            if durable.path_acceptance is None:
                raise ValueError("saved acceptance file baseline is missing")
            path_baseline, snapshot_error = durable.path_acceptance
        else:
            path_baseline, snapshot_error = (
                (None, None) if request.isolate or path_error else _snapshot_paths(Path(task.repo_path), contract_paths)
            )
        if durable and not request.isolate and contract_paths:
            durable.path_acceptance = (path_baseline, snapshot_error)
        acceptance_setup_error = path_error or snapshot_error
        config = dataclasses.replace(
            self.config,
            durable_run=durable,
            hooks=request.hooks if request.hooks is not None else self.config.hooks,
            cancel_event=(
                request.cancel_event
                if request.cancel_event is not None
                else self.config.cancel_event
            ),
            prepare_next_turn=(
                request.prepare_next_turn
                if request.prepare_next_turn is not None
                else self.config.prepare_next_turn
            ),
        )
        memory = self._memory_runtime(task.repo_path, config, entrypoint)
        config = dataclasses.replace(config, memory_runtime=memory)
        if config.run_profile == "sandboxed" and not request.sandbox:
            raise ValueError("sandboxed run profile requires Docker sandbox")
        if config.run_profile == "safe":
            request = dataclasses.replace(request, execution_constraints=ExecutionConstraints(read_only=True))
        if config.run_profile == "benchmark":
            config = dataclasses.replace(
                config,
                max_model_calls=config.max_model_calls or config.max_steps,
                max_total_tokens=config.max_total_tokens or 100_000,
            )
        coordinator = None
        owned_parent_engine = None

        if request.isolate:
            if request.history is not None:
                raise ValueError("isolated runs do not support shared history")
            if self.registry_builder is None:
                raise ValueError("isolated runs require registry_builder")
            engine = self.engine or TaskEngine(Path(self.log_dir) / "tasks.db")
            trace_path = None

            def log_created(_task_id: str, path: str) -> None:
                nonlocal trace_path
                trace_path = path

            try:
                # ContextVar propagation lets orchestrate_run/EventLog.create inherit
                # product metadata without adding product concerns to orchestrator APIs.
                with bind_trace_context(
                    entrypoint=entrypoint,
                    session_id=request.session_id,
                ):
                    result = asyncio.run(orchestrate_run(
                        backend=self.backend,
                        task=task,
                        engine=engine,
                        registry_builder=self.registry_builder,
                        bus=self.bus,
                        log_dir=self.log_dir,
                        sandbox=request.sandbox,
                        config=config,
                        confirm_callback=self.confirm_callback,
                        result_policy=request.result_policy,
                        on_log_created=log_created,
                        on_event=on_event,
                        mcp_config=None if config.run_profile == "safe" else self.mcp_config,
                        execution_constraints=request.execution_constraints,
                        coordinator_factory=(self._make_coordinator if config.multi_agent_enabled else None),
                    ))
            except BaseException as exc:
                if trace_path:
                    _record_exception_trace(
                        trace_path,
                        task_id=task.task_id,
                        entrypoint=entrypoint,
                        session_id=request.session_id,
                        error=exc,
                    )
                if memory is not None and config.memory_config.auto_capture:
                    memory.record_exception(task, exc, trace_path=trace_path, isolate=True)
                raise

            _merge_context_policy_usage(result, config.prepare_next_turn)
            _normalize_isolated_failure(result)
            # Orchestrator attaches worker usage before returning.
            result.trace_path = trace_path
            _apply_independent_acceptance(
                acceptance,
                task,
                result,
                contract_paths,
                path_baseline,
                acceptance_setup_error,
                durable=durable,
            )
            if memory is not None and config.memory_config.auto_capture and isinstance(result, RunResult):
                try:
                    if trace_path:
                        with EventLog.open_existing(trace_path, task_id=task.task_id,
                                                    entrypoint=entrypoint, session_id=request.session_id) as memory_log:
                            memory.conclude(result, task=task, acceptance=result.acceptance_status,
                                            trace_path=trace_path, isolate=True, log=memory_log)
                    else:
                        memory.conclude(result, task=task, acceptance=result.acceptance_status,
                                        trace_path=None, isolate=True)
                except Exception as exc:  # noqa: BLE001 - memory must not change the run result
                    logger.warning("Isolated memory finalization failed: %s", exc)
            if trace_path:
                _record_post_run_trace(
                    trace_path,
                    result=result,
                    acceptance_requested=acceptance.has_independent_checks(),
                    entrypoint=entrypoint,
                    session_id=request.session_id,
                    on_event=on_event,
                )
            return result

        if config.run_profile != "safe":
            self._ensure_direct_mcp()
        if memory is not None:
            from project_memory.tools import MemoryProposeTool, MemoryReadTool, MemorySearchTool
            for tool in (MemorySearchTool(memory), MemoryReadTool(memory)):
                self.registry.unregister(tool.name)
                self.registry.register(tool)
            self.registry.unregister("memory_propose")
            if entrypoint != "worker" and config.memory_config.auto_capture:
                self.registry.register(MemoryProposeTool(memory, run_id=task.task_id,
                    session_id=request.session_id))
        else:
            for name in ("memory_search", "memory_read", "memory_propose"):
                self.registry.unregister(name)
        if config.multi_agent_enabled:
            worker_engine = self.engine or self._worker_engine()
            try:
                parent = worker_engine.get_task(task.task_id)
                if request.resume and parent.owner == "lead":
                    owned_parent_engine = worker_engine
            except TaskNotFound:
                worker_engine.create_task(task.description[:80], task.description,
                                          agent_id="lead", role="lead", task_id=task.task_id)
                worker_engine.claim_task(task.task_id, owner="lead")
                owned_parent_engine = worker_engine
            coordinator = self._make_coordinator(
                task.repo_path, task.task_id, config,
                worker_engine,
            )
            coordinator.register_tools(self.registry)
            config = dataclasses.replace(config, worker_coordinator=coordinator)
        else:
            from agent.coordinator import DelegateTasksTool
            if isinstance(self.registry.get_tool("delegate_tasks"), DelegateTasksTool):
                self.registry.unregister("delegate_tasks")
        if coordinator is None or coordinator.background is None:
            from agent.background_workers import WorkerStatusTool
            for name in ("worker_status", "worker_wait"):
                if isinstance(self.registry.get_tool(name), WorkerStatusTool):
                    self.registry.unregister(name)
        executor = ToolExecutor(
            self.registry,
            hooks=config.hooks,
            permission=request.permission or PermissionManager(
                workspace=task.repo_path, registry=self.registry,
                constraints=request.execution_constraints,
                approval_mode=config.approval_mode,
            ),
            confirm_callback=self.confirm_callback,
            constraints=request.execution_constraints,
        )
        key = (
            id(config.hooks),
            id(config.cancel_event),
            id(config.prepare_next_turn),
            id(request.permission),
            config.repo_map_mode,
            config.repo_map_budget,
            config.repo_map_summary_budget,
            config.repo_map_cache_dir,
            config.run_profile,
            config.finish_policy,
            config.multi_agent_enabled,
            config.worker_mode,
            config.max_model_calls,
            config.max_total_tokens,
            request.execution_constraints,
        )
        if key != self._agent_key:
            if self._agent_key is not None:
                self.agent = Agent(self.backend, self.registry, config, executor=executor)
            self._agent_key = key
        self.agent._executor = executor
        self.agent._cfg = config

        own_log = log is None
        log = log or EventLog.create(
            task,
            log_dir=self.log_dir,
            session_id=request.session_id,
            entrypoint=entrypoint,
        )
        log.configure_trace(entrypoint=entrypoint, session_id=request.session_id)
        proposal = self.registry.get_tool("memory_propose")
        if proposal is not None and hasattr(proposal, "log"):
            proposal.log = log
            proposal.trace_path = str(log.path)
        if on_event is not None:
            log.on_append(on_event)
        self._record_mcp_capabilities(log)
        if memory is not None and request.history is not None and request.history.message_count > 1 and config.prepare_next_turn is not None:
            self.agent._memory_context = memory.context(task.description,
                TokenBudget(total=config.budget_tokens).counter, log=log)
        try:
            if coordinator is not None and coordinator.background is not None:
                coordinator.background.attach(log)
            preparation_result = None if request.resume else self._prepare_shared_history_boundary(
                task=task,
                history=request.history,
                callback=config.prepare_next_turn,
                log=log,
                config=config,
            )
            result = (
                preparation_result
                if preparation_result is not None
                else self.agent.run(task, log, history=request.history)
            )
            _merge_context_policy_usage(result, config.prepare_next_turn)
            if coordinator is not None:
                if coordinator.background is not None:
                    coordinator.background.close(preserve=result.termination_reason == "paused")
                _merge_worker_usage(result, coordinator, log)
            result.trace_path = str(log.path)
            _apply_independent_acceptance(
                acceptance,
                task,
                result,
                contract_paths,
                path_baseline,
                acceptance_setup_error,
                durable=durable,
            )
            if memory is not None and config.memory_config.auto_capture:
                memory.conclude(result, task=task, acceptance=result.acceptance_status,
                                trace_path=str(log.path), log=log)
            _record_post_run_trace_log(
                log,
                result=result,
                acceptance_requested=acceptance.has_independent_checks(),
            )
            if owned_parent_engine is not None:
                (owned_parent_engine.complete_task if result.is_success() else owned_parent_engine.fail_task)(task.task_id)
            return result
        except BaseException as exc:
            if memory is not None and config.memory_config.auto_capture:
                memory.record_exception(task, exc, trace_path=str(log.path))
            try:
                log.log_run_exception(task_id=task.task_id, error=exc)
            except Exception as trace_exc:  # noqa: BLE001 — trace is best-effort
                logger.warning("Failed to record runner exception trace: %s", trace_exc)
            if owned_parent_engine is not None and durable is None:
                owned_parent_engine.fail_task(task.task_id)
            raise
        finally:
            if coordinator is not None and coordinator.background is not None:
                coordinator.background.close(preserve=durable is not None)
            if on_event is not None:
                log.on_append(None)
            if own_log:
                log.close()

    def _make_coordinator(self, repo_path, parent_task_id, config, engine):
        if self.worker_backend_factory is None:
            raise ValueError("multi-agent requires a fresh worker backend factory")
        from agent.coordinator import AgentCoordinator
        if self.registry_builder is None:
            from entry.cli import _build_run_registry
            registry_builder = _build_run_registry
        else:
            registry_builder = self.registry_builder
        coordinator = AgentCoordinator(
            repo_path=repo_path, log_dir=self.log_dir, parent_task_id=parent_task_id,
            backend_factory=self.worker_backend_factory, registry_factory=registry_builder,
            agent_config=dataclasses.replace(config, durable_run=None),
            parent_run_id=config.durable_run.run_id if config.durable_run else None,
            parent_owner=config.durable_run.owner if config.durable_run else None,
            durable=config.durable_run,
            engine=engine, cancel_event=config.cancel_event,
            max_workers=config.max_workers, max_tasks=config.max_delegate_tasks,
            total_tokens=config.total_worker_tokens, timeout_seconds=config.worker_timeout_seconds,
            sandbox_workers=config.run_profile == "sandboxed",
        )
        if config.durable_run and config.durable_run.restoring and coordinator.background is None:
            coordinator.restore_delegation_usage(config.durable_run)
        return coordinator

    @staticmethod
    def _memory_runtime(repo_path, config, entrypoint):
        settings = config.memory_config
        if settings is None or not settings.enabled or (entrypoint == "eval" and not settings.evaluation_enabled):
            return None
        try:
            from project_memory.runtime import MemoryRuntime
            return MemoryRuntime(repo_path, root=settings.root,
                                 budget=settings.context_budget_tokens, max_results=settings.max_results,
                                 defer_promotion=settings.defer_promotion)
        except Exception as exc:  # noqa: BLE001 - optional memory cannot stop a run
            logger.warning("Memory initialization failed: %s", exc)
            return None

    def _worker_engine(self):
        if self._owned_worker_engine is None:
            self._owned_worker_engine = TaskEngine(Path(self.log_dir) / "tasks.db")
        return self._owned_worker_engine

    def _record_mcp_capabilities(self, log: EventLog) -> None:
        if self._mcp_manager is None:
            return
        for snapshot in self._mcp_manager.snapshots:
            log.log_trace(
                EventType.MCP_SERVER_CAPABILITIES,
                0,
                **dataclasses.asdict(snapshot),
            )

    def _ensure_direct_mcp(self) -> None:
        if self._mcp_manager is None and self.mcp_config is not None and self.mcp_config.enabled:
            self._mcp_manager = attach_mcp_tools(self.registry, self.mcp_config)

    def close(self) -> None:
        """Close long-lived external capability connections owned by this Runner."""
        if self._mcp_manager is not None:
            manager, self._mcp_manager = self._mcp_manager, None
            manager.close()
        if self._owned_worker_engine is not None:
            self._owned_worker_engine.close()
            self._owned_worker_engine = None

    def _prepare_shared_history_boundary(
        self,
        *,
        task: Task,
        history: ConversationHistory | None,
        callback: PrepareNextTurn | None,
        log: EventLog,
        config: AgentConfig,
    ) -> RunResult | None:
        """已有共享历史的新 run 在第一次模型调用前复用 Agent prepare 生命周期。

        Fresh task / fresh Chat round 只有当前 user message，不进入该路径；Agent.run
        内部 step>1 继续调用相同的 ``Agent._prepare_next_turn``。因此首轮共享历史
        与后续 turn 对 prepare failure / cancel / Trace 使用同一生产语义。
        """
        if history is None or history.message_count <= 1 or callback is None:
            return None

        # Shared-history preflight runs before Agent.run; clear the previous run's
        # transient plan so context pressure can never observe stale planning state.
        self.agent.reset_run_runtime_state()
        self.agent._current_repo_path = task.repo_path
        repo_map = self.agent.prepare_repository(task, chat_turn=log.entrypoint == "chat")

        token_budget = TokenBudget(
            total=config.budget_tokens,
            repo_map_cap=config.repo_map_budget,
        )
        return self.agent._prepare_next_turn(
            task,
            1,
            history,
            repo_map,
            token_budget,
            log,
            0,
            SessionUsage(),
        )


def _normalize_isolated_failure(result: RunResult) -> None:
    """Fill the one structured classification orchestrator cannot currently emit.

    Sandbox preflight is a deterministic framework/runtime startup failure that happens
    before Agent.run. Keep orchestrator APIs unchanged, but make the product Runner
    expose the same ``infrastructure_error`` termination taxonomy as direct runs.
    """
    if (
        result.status is RunStatus.FAILED
        and result.termination_reason is None
        and (result.error or "").startswith("Sandbox preflight failed:")
    ):
        result.termination_reason = "infrastructure_error"


def _resolve_entrypoint(request: RunRequest) -> str:
    """Label a run for tracing, using the request's explicit label when supplied."""
    if request.entrypoint:
        return request.entrypoint
    if request.task.issue_url:
        return "github_issue"
    if request.history is not None or request.session_id is not None:
        return "chat"
    return "cli"


def _record_post_run_trace_log(
    log: EventLog,
    *,
    result: RunResult,
    acceptance_requested: bool,
) -> None:
    """Trace must never replace the execution result."""
    try:
        log.log_acceptance(result, requested=acceptance_requested)
        log.log_run_termination(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record runner completion trace: %s", exc)


def _record_post_run_trace(
    trace_path: str,
    *,
    result: RunResult,
    acceptance_requested: bool,
    entrypoint: str,
    session_id: str | None,
    on_event=None,
) -> None:
    try:
        with EventLog.open_existing(
            trace_path,
            task_id=result.task_id,
            entrypoint=entrypoint,
            session_id=session_id,
        ) as trace_log:
            if on_event is not None:
                trace_log.on_append(on_event)
            _record_post_run_trace_log(
                trace_log,
                result=result,
                acceptance_requested=acceptance_requested,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to append isolated runner trace: %s", exc)


def _record_exception_trace(
    trace_path: str,
    *,
    task_id: str,
    entrypoint: str,
    session_id: str | None,
    error: BaseException,
) -> None:
    try:
        with EventLog.open_existing(
            trace_path,
            task_id=task_id,
            entrypoint=entrypoint,
            session_id=session_id,
        ) as trace_log:
            trace_log.log_run_exception(task_id=task_id, error=error)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to append isolated exception trace: %s", exc)


def _apply_independent_acceptance(
    contract: AcceptanceContract,
    task: Task,
    result: RunResult,
    contract_paths: set[str],
    path_baseline: dict[str, str] | None,
    setup_error: str | None,
    *,
    durable=None,
) -> None:
    if durable is not None and result.termination_reason == "paused":
        return
    if durable is None:
        _check_independent_acceptance(contract, task, result, contract_paths, path_baseline, setup_error)
        return
    step = result.steps_taken + 1
    state = durable.load_checkpoint()
    state.pop("pending_model_call", None)
    state["agent_result"] = result.to_dict()
    if contract.has_independent_checks():
        from context.repository_state import repository_content_fingerprint

        workspace = result.worktree.path if result.worktree else task.repo_path
        excluded = (result.trace_path,) if result.trace_path else ()
        state["acceptance_inputs"] = {
            "paths": sorted(contract_paths), "baseline": path_baseline, "setup_error": setup_error,
            "workspace_digest": repository_content_fingerprint(workspace, excluded_paths=excluded) if workspace else None,
        }
        durable.checkpoint(step, "acceptance_pending", state, status="acceptance_pending")
    _check_independent_acceptance(contract, task, result, contract_paths, path_baseline, setup_error)
    status = "acceptance_" + {"not_requested": "skipped"}.get(result.acceptance_status, result.acceptance_status)
    durable.checkpoint(step, status, {**state, "acceptance_status": result.acceptance_status,
                                     "acceptance_error": result.acceptance_error}, status=status)


def _resume_independent_acceptance(durable, contract: AcceptanceContract, task: Task, state: dict,
                                   entrypoint: str, session_id: str | None, on_event) -> RunResult:
    from context.repository_state import repository_content_fingerprint
    from durable_runtime.store import StoreFormatError

    result = RunResult.from_dict(state["agent_result"])
    inputs = state.get("acceptance_inputs")
    workspace = result.worktree.path if result.worktree else task.repo_path
    excluded = (result.trace_path,) if result.trace_path else ()
    if inputs and workspace and (not Path(workspace).is_dir() or
                                 repository_content_fingerprint(workspace, excluded_paths=excluded) != inputs["workspace_digest"]):
        raise StoreFormatError("workspace changed since the independent acceptance checkpoint")
    if "acceptance_status" in state:
        result.acceptance_status = state["acceptance_status"]
        result.acceptance_error = state.get("acceptance_error")
        return result
    if not inputs or contract.verifier is not None:
        raise StoreFormatError("saved independent acceptance inputs are unavailable")
    _check_independent_acceptance(contract, task, result, set(inputs["paths"]), inputs["baseline"], inputs["setup_error"])
    status = f"acceptance_{result.acceptance_status}"
    durable.checkpoint(state["next_step"], status, {**state, "acceptance_status": result.acceptance_status,
                                                    "acceptance_error": result.acceptance_error}, status=status)
    if result.trace_path:
        _record_post_run_trace(result.trace_path, result=result, acceptance_requested=True,
                               entrypoint=entrypoint, session_id=session_id, on_event=on_event)
    return result


def _check_independent_acceptance(
    contract: AcceptanceContract,
    task: Task,
    result: RunResult,
    contract_paths: set[str],
    path_baseline: dict[str, str] | None,
    setup_error: str | None,
) -> None:
    """在 Agent 返回后执行路径约束和隐藏 verifier，并保留两层独立状态。"""
    if not contract.has_independent_checks():
        return
    if not result.is_success():
        result.acceptance_status = "skipped"
        result.acceptance_error = f"agent status is {result.status.value}"
        return
    if setup_error:
        result.acceptance_status, result.acceptance_error = "failed", setup_error
        return

    workspace = (
        Path(result.worktree.path)
        if result.worktree and result.worktree.path
        else Path(task.repo_path)
    )
    if result.worktree and result.worktree.path is None:
        result.acceptance_status = "failed"
        result.acceptance_error = "worktree is unavailable for independent acceptance"
        return

    if contract.required_paths or contract.forbidden_paths:
        changed_paths, error = _changed_paths(
            result,
            workspace,
            contract_paths,
            path_baseline,
        )
        if error:
            result.acceptance_status, result.acceptance_error = "failed", error
            return
        required_paths = {path.replace("\\", "/") for path in contract.required_paths}
        forbidden_paths = {path.replace("\\", "/") for path in contract.forbidden_paths}
        missing = sorted(required_paths - changed_paths)
        forbidden = sorted(forbidden_paths & changed_paths)
        if missing or forbidden:
            reasons = (
                [f"required paths not changed: {', '.join(missing)}"] if missing else []
            ) + (
                [f"forbidden paths changed: {', '.join(forbidden)}"] if forbidden else []
            )
            result.acceptance_status = "failed"
            result.acceptance_error = "; ".join(reasons)
            return

    if contract.verifier is not None:
        try:
            verified = bool(contract.verifier(workspace))
        except Exception as exc:
            result.acceptance_status = "failed"
            result.acceptance_error = (
                f"hidden verifier raised {type(exc).__name__}: {exc}"
            )
            return
        if not verified:
            result.acceptance_status = "failed"
            result.acceptance_error = "hidden verifier returned false"
            return
    result.acceptance_status = "passed"


def _changed_paths(
    result: RunResult,
    workspace: Path,
    contract_paths: set[str],
    path_baseline: dict[str, str] | None,
) -> tuple[set[str], str | None]:
    """隔离运行读取产物，普通运行只比较契约文件的前后内容指纹。"""
    if result.worktree is not None:
        return {
            path.replace("\\", "/") for path in result.worktree.changed_files
        }, None
    current, error = _snapshot_paths(workspace, contract_paths)
    if error:
        return set(), error
    return {
        path
        for path in contract_paths
        if path_baseline and path_baseline[path] != current[path]
    }, None


def _normalize_contract_paths(paths: tuple[str, ...]) -> tuple[set[str], str | None]:
    """把契约路径统一为安全的 POSIX 相对路径。"""
    normalized = set()
    for raw in paths:
        path = PurePosixPath(raw.replace("\\", "/"))
        if not raw or path.is_absolute() or ".." in path.parts:
            return set(), f"invalid acceptance path: {raw!r}"
        normalized.add(path.as_posix())
    return normalized, None


def _snapshot_paths(root: Path, paths: set[str]) -> tuple[dict[str, str], str | None]:
    """只读取契约指定文件，避免把运行前已有的用户修改计入本轮结果。"""
    root = root.resolve()
    states = {}
    for relative in paths:
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            return {}, f"acceptance path escapes repository: {relative}"
        if target.is_dir():
            return {}, f"acceptance path must be a file: {relative}"
        try:
            states[relative] = (
                hashlib.sha256(target.read_bytes()).hexdigest()
                if target.exists()
                else "missing"
            )
        except OSError as exc:
            return {}, f"cannot inspect acceptance path {relative}: {exc}"
    return states, None
