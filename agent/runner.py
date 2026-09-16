"""Shared execution composition root for CLI, Chat, API, and fix-pr."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from agent.core import Agent, AgentConfig, PrepareNextTurn, PrepareNextTurnContext
from agent.event_log import EventLog
from agent.orchestrate import orchestrate_run
from agent.task import RunResult, Task
from agent.trace_v2 import bind_trace_context
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from harness import Hooks, PermissionManager, ToolExecutor
from runtime.worktree import WorktreeResultPolicy
from task.engine import TaskEngine
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.config = config or AgentConfig()
        self.log_dir = log_dir
        self.registry_builder = registry_builder
        self.engine = engine
        self.confirm_callback = confirm_callback
        self.bus = bus
        executor = ToolExecutor(
            registry,
            hooks=self.config.hooks,
            confirm_callback=confirm_callback,
        )
        self.agent = Agent(backend, registry, self.config, executor=executor)
        self._agent_key = (
            id(self.config.hooks),
            id(self.config.cancel_event),
            id(self.config.prepare_next_turn),
            id(None),
        )

    def run(
        self,
        request: RunRequest,
        *,
        log: EventLog | None = None,
        on_event=None,
        on_log_created=None,
    ) -> RunResult:
        acceptance = request.acceptance or AcceptanceContract.from_task(request.task)
        task = acceptance.apply(request.task)
        entrypoint = _resolve_entrypoint(request)
        contract_paths, path_error = _normalize_contract_paths(
            acceptance.required_paths + acceptance.forbidden_paths
        )
        path_baseline, snapshot_error = (
            (None, None)
            if request.isolate or path_error
            else _snapshot_paths(Path(task.repo_path), contract_paths)
        )
        acceptance_setup_error = path_error or snapshot_error
        config = dataclasses.replace(
            self.config,
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

        if request.isolate:
            if request.history is not None:
                raise ValueError("isolated runs do not support shared history")
            if self.registry_builder is None:
                raise ValueError("isolated runs require registry_builder")
            engine = self.engine or TaskEngine(Path(self.log_dir) / "tasks.db")
            trace_path = None

            def log_created(task_id: str, path: str) -> None:
                nonlocal trace_path
                trace_path = path
                if on_log_created is not None:
                    on_log_created(task_id, path)

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
                raise

            result.trace_path = trace_path
            _apply_independent_acceptance(
                acceptance,
                task,
                result,
                contract_paths,
                path_baseline,
                acceptance_setup_error,
            )
            if trace_path:
                _record_post_run_trace(
                    trace_path,
                    result=result,
                    acceptance_requested=acceptance.has_independent_checks(),
                    entrypoint=entrypoint,
                    session_id=request.session_id,
                )
            return result

        executor = ToolExecutor(
            self.registry,
            hooks=config.hooks,
            permission=request.permission,
            confirm_callback=self.confirm_callback,
        )
        key = (
            id(config.hooks),
            id(config.cancel_event),
            id(config.prepare_next_turn),
            id(request.permission),
        )
        if key != self._agent_key:
            self.agent = Agent(self.backend, self.registry, config, executor=executor)
            self._agent_key = key

        own_log = log is None
        log = log or EventLog.create(
            task,
            log_dir=self.log_dir,
            session_id=request.session_id,
            entrypoint=entrypoint,
        )
        log.configure_trace(entrypoint=entrypoint, session_id=request.session_id)
        if on_log_created is not None:
            on_log_created(task.task_id, str(log.path))
        if on_event is not None:
            log.on_append(on_event)
        try:
            self._prepare_shared_history_boundary(
                task=task,
                history=request.history,
                callback=config.prepare_next_turn,
                log=log,
                config=config,
            )
            result = self.agent.run(task, log, history=request.history)
            result.trace_path = str(log.path)
            _apply_independent_acceptance(
                acceptance,
                task,
                result,
                contract_paths,
                path_baseline,
                acceptance_setup_error,
            )
            _record_post_run_trace_log(
                log,
                result=result,
                acceptance_requested=acceptance.has_independent_checks(),
            )
            return result
        except BaseException as exc:
            try:
                log.log_run_exception(task_id=task.task_id, error=exc)
            except Exception as trace_exc:  # noqa: BLE001 — trace is best-effort
                logger.warning("Failed to record runner exception trace: %s", trace_exc)
            raise
        finally:
            if on_event is not None:
                log.on_append(None)
            if own_log:
                log.close()

    def _prepare_shared_history_boundary(
        self,
        *,
        task: Task,
        history: ConversationHistory | None,
        callback: PrepareNextTurn | None,
        log: EventLog,
        config: AgentConfig,
    ) -> None:
        """已有共享历史的新 run 在第一次模型调用前复用同一 Context policy。

        Fresh task / fresh Chat round 只有当前 user message，不进入该路径；Agent.run
        内部原有 step>1 prepare_next_turn 生命周期保持不变。
        """
        if history is None or history.message_count <= 1 or callback is None:
            return

        self.agent._current_repo_path = task.repo_path
        self.agent._repo_map_query = task.description
        cache_key = task.repo_path
        if self.agent._repo_map_cache_key != cache_key:
            self.agent.invalidate_repo_map_cache()
            self.agent._repo_map_force_refresh = False
            self.agent._repo_map_cache_key = cache_key
            self.agent._repo_map_instance = RepoMap(task.repo_path)
        elif getattr(self.agent, "_repo_map_cache_query", None) != task.description:
            if hasattr(self.agent, "_repo_map_cache"):
                del self.agent._repo_map_cache

        token_budget = TokenBudget(total=config.budget_tokens)
        repo_map = getattr(self.agent, "_repo_map_instance", RepoMap(task.repo_path))
        system_content, repo_map_content, schemas = self.agent._render_request_parts(
            token_budget,
            repo_map,
        )
        context = PrepareNextTurnContext(
            task=task,
            step=1,
            history=history,
            repo_map=repo_map,
            token_budget=token_budget,
            cancel_event=config.cancel_event,
            event_log=log,
            system_content=system_content,
            repo_map_content=repo_map_content,
            tool_schemas=schemas,
        )
        prepared = callback(context)
        if prepared is None:
            return
        if prepared.refresh_repo_map:
            self.agent.invalidate_repo_map_cache(task.repo_path)
        if prepared.messages:
            history.add_many(list(prepared.messages))
        if prepared.history_override is not None:
            self.agent._prepared_history_override = prepared.history_override


def _resolve_entrypoint(request: RunRequest) -> str:
    """Resolve the four product entrypoints without breaking existing callers."""
    if request.entrypoint:
        return request.entrypoint
    if request.task.issue_url:
        return "github_issue"
    if request.cancel_event is not None:
        return "api"
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
) -> None:
    try:
        with EventLog.open_existing(
            trace_path,
            task_id=result.task_id,
            entrypoint=entrypoint,
            session_id=session_id,
        ) as trace_log:
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
