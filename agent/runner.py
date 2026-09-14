"""Shared execution composition root for CLI, Chat, API, and fix-pr."""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent.core import Agent, AgentConfig, PrepareNextTurn
from agent.event_log import EventLog
from agent.orchestrate import orchestrate_run
from agent.task import RunResult, Task
from context.history import ConversationHistory
from harness import Hooks, PermissionManager, ToolExecutor
from runtime.worktree import WorktreeResultPolicy
from task.engine import TaskEngine
from tools.base import ToolRegistry


@dataclass(frozen=True)
class AcceptanceContract:
    require_changes: bool = False
    require_tests: bool = False

    @classmethod
    def from_task(cls, task: Task) -> "AcceptanceContract":
        return cls(task.require_changes, task.require_tests)

    def apply(self, task: Task) -> Task:
        return dataclasses.replace(
            task,
            require_changes=self.require_changes,
            require_tests=self.require_tests,
        )


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
        task = (request.acceptance or AcceptanceContract.from_task(request.task)).apply(
            request.task
        )
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
            result.trace_path = trace_path
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
        )
        if on_log_created is not None:
            on_log_created(task.task_id, str(log.path))
        if on_event is not None:
            log.on_append(on_event)
        try:
            result = self.agent.run(task, log, history=request.history)
            result.trace_path = str(log.path)
            return result
        finally:
            if on_event is not None:
                log.on_append(None)
            if own_log:
                log.close()
