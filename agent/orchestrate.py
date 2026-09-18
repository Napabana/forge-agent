"""
agent/orchestrate.py

M4 主循环集成：把 M1（TaskEngine + WorktreeSession）、M2（AgentBus +
ToolExecutor/Permission）、M3（DockerRuntime 加固）接进 ReAct 主循环。

核心设计——同步/异步阻抗失配的解法：
- Agent.run() 是同步 ReAct 循环（清晰可测，CLAUDE.md 约束保持同步）。
- WorktreeSession / AgentBus 是 async。
- 解法：本模块提供一个 async 组合根 orchestrate_run()，显式管理
  WorktreeSession 的创建、成果检查与最终处置。Agent.run() 同步执行，
  EventLog 事件先进入队列，运行结束后由 forwarder 转发到 bus。

safe_path 注入：PermissionManager(workspace=str(wt.path)) 在 executor 层
强制文件路径不逃逸 worktree（读写工具都覆盖）。不依赖 LLM 填的 params。

事件审计：orchestrator 在 worktree 生命周期 + 权限决策点上写新事件类型
（WORKTREE_CREATED/RETAINED/REMOVED、PERMISSION_DECISION、TASK_CLAIMED）
到 EventLog，
既有 ACTION/OBSERVATION/REFLECTION 由同步循环照常写。

用法（见 scripts/m4_demo.py）：
    engine = TaskEngine("tasks.db")
    result = asyncio.run(orchestrate_run(
        backend=..., task=task, engine=engine,
        registry_builder=build_registry, log_dir="./logs",
    ))
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import Any, Callable

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import RunResult, RunStatus, Task
from harness.executor import ToolExecutor
from harness.permission import PermissionDecision, PermissionManager
from ipc.bus import AgentBus
from runtime.worktree import (
    WorktreeArtifact,
    WorktreeDisposition,
    WorktreeFinalizeAction,
    WorktreeResultPolicy,
    WorktreeSession,
)
from task.engine import TaskEngine
from tools.runtime import CONTAINER_WORKDIR, DockerRuntime, LocalRuntime, Runtime

logger = logging.getLogger(__name__)


# registry_builder 契约：(config, confirm_callback, runtime, *, default_cwd, workspace) -> ToolRegistry
# 注入是为了避免 agent 层 import entry/cli（拉入 Click 依赖）。
RegistryBuilder = Callable[..., Any]

# agent_factory 契约：(backend, registry, config, executor) -> Agent
# 默认造真 Agent；测试可注入 FakeAgent。
AgentFactory = Callable[..., Agent]
LogCreatedCallback = Callable[[str, str], None]


class SandboxPreflightError(RuntimeError):
    """Sandbox is running but lacks required tools or worktree visibility."""


def _default_agent_factory(backend, registry, config, executor) -> Agent:
    return Agent(backend, registry, config=config, executor=executor)


async def _bus_forwarder(
    q: asyncio.Queue, bus: AgentBus, task_id: str,
) -> None:
    """把 EventLog 的 on_append 事件转发到 AgentBus。

    从 asyncio.Queue 取 Event，publish 到 `events.{event_type}` topic。
    None 是哨兵：收到即退出。Agent.run 同步执行，on_append 先把事件放入
    queue，运行结束后 forwarder drain 并 publish。
    """
    while True:
        ev = await q.get()
        if ev is None:
            return
        try:
            await bus.publish(
                f"events.{ev.event_type.value}",
                sender="orchestrator",
                content=ev.to_dict(),
                metadata={"task_id": task_id},
            )
        except Exception as exc:  # noqa: BLE001 — bus 故障不能影响运行
            logger.warning("[orchestrate] bus forward error: %s", exc)


async def _stop_forwarder(q: asyncio.Queue | None, task: asyncio.Task | None) -> None:
    """哨兵终止 forwarder 并等它退出。None 时空操作。"""
    if q is None or task is None:
        return
    try:
        q.put_nowait(None)
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    except Exception:  # noqa: BLE001
        pass
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _finalize_worktree(
    session: WorktreeSession,
    policy: WorktreeResultPolicy,
    *,
    partial: bool,
) -> WorktreeArtifact:
    """把用户结果策略转换为一次明确的 worktree 最终动作。"""
    changes = await session.inspect_changes()
    action = WorktreeFinalizeAction.DISCARD
    if policy == WorktreeResultPolicy.KEEP_IF_CHANGED and changes.has_changes:
        action = WorktreeFinalizeAction.RETAIN
    return await session.finalize(action, changes=changes, partial=partial)


async def _record_worktree_finalized(
    *,
    log: EventLog,
    bus: AgentBus | None,
    task_id: str,
    name: str,
    artifact: WorktreeArtifact,
    reason: str,
) -> None:
    """记录真实的 worktree 最终状态；bus 故障沿用既有传播语义。"""
    if artifact.disposition == WorktreeDisposition.RETAINED:
        log.log_worktree_retained(
            task_id,
            artifact.branch,
            artifact.path or "",
            reason=reason,
            changed_files=list(artifact.changed_files),
        )
        if bus is not None:
            await bus.publish(
                "worktree.retained",
                sender="orchestrator",
                content={
                    "task_id": task_id,
                    "path": artifact.path,
                    "branch": artifact.branch,
                    "reason": reason,
                    "changed_files": list(artifact.changed_files),
                },
            )
        return

    log.log_worktree_removed(task_id, name, "", reason=reason)
    if bus is not None:
        await bus.publish(
            "worktree.removed",
            sender="orchestrator",
            content={"task_id": task_id, "reason": reason},
        )


async def orchestrate_run(
    *,
    backend,
    task: Task,
    engine: TaskEngine,
    registry_builder: RegistryBuilder,
    bus: AgentBus | None = None,
    log_dir: str = "./logs",
    worktree_name: str | None = None,
    sandbox: bool = False,
    sandbox_image: str | None = None,
    readonly_root: bool = True,
    config: AgentConfig | None = None,
    confirm_callback: Callable[[str], bool] | None = None,
    agent_factory: AgentFactory | None = None,
    on_log_created: LogCreatedCallback | None = None,
    on_event: Callable[[Any], None] | None = None,
    result_policy: WorktreeResultPolicy | str = WorktreeResultPolicy.KEEP_IF_CHANGED,
) -> RunResult:
    """
    在隔离的 git worktree 内跑一次完整的 ReAct 循环，全程 TaskEngine 记账 +
    EventLog 审计 + 可选 AgentBus 广播。

    Args:
        backend:          LLM 后端
        task:             任务描述（task.repo_path 是宿主仓库根）
        engine:           TaskEngine（SQLite 状态机）
        registry_builder: 构造 ToolRegistry 的回调（避免 agent 层依赖 entry/cli）
        bus:              可选 AgentBus；None=不广播
        log_dir:          JSONL 日志目录
        worktree_name:    worktree 名；默认 f"task-{task_id}"
        sandbox:          True=用 DockerRuntime（M3 加固）；False=LocalRuntime
        sandbox_image:    Docker 镜像；None=默认 SANDBOX_IMAGE
        readonly_root:    sandbox 模式下传给 DockerRuntime 的只读根
        config:           AgentConfig；None=默认
        confirm_callback: CONFIRM 决策的确认回调
        agent_factory:    构造 Agent 的回调（测试注入 FakeAgent）
        result_policy:    discard=始终清理；keep-if-changed=有成果时保留 worktree

    Returns:
        RunResult（agent 的最终结果）
    """
    agent_cfg = config or AgentConfig()
    # 正常运行时调用默认工厂；测试可注入 agent_factory。
    factory = agent_factory or _default_agent_factory
    policy = WorktreeResultPolicy(result_policy)

    # 1. TaskEngine 记账：创建任务 → 认领。TaskEngine 为 task_id 权威源。
    task_id = engine.create_task(
        subject=task.description[:80],
        description=task.description,
    )
    task.task_id = task_id   # 让 EventLog / RunResult 的 task_id 与 DB 一致
    engine.claim_task(task_id, owner="agent")

    # 2. EventLog + bus 生命周期事件
    log = EventLog.create(task, log_dir=log_dir)
    if on_log_created is not None:
        try:
            on_log_created(task_id, str(log.path))
        except Exception as exc:  # noqa: BLE001 - observer must not break run
            logger.warning("[orchestrate] on_log_created error: %s", exc)
    log.log_task_claimed(task_id, owner="agent")
    if bus is not None:
        await bus.publish(
            "tasks.claimed", sender="orchestrator",
            content={"task_id": task_id},
            metadata={"subject": task.description[:80]},
        )

    wt_name = worktree_name or f"task-{task_id}"

    # bus 逐步事件转发：on_append → queue → forwarder 协程。
    # 同步循环每写一条 event 就 put_nowait，forwarder 异步 publish 到 events.* 。
    bus_q: asyncio.Queue | None = None
    # 把 EventLog.on_append 绑定到队列，再由 _bus_forwarder 异步发布，
    # 避免同步 Agent.run() 与异步 AgentBus 直接耦合。
    forwarder_task: asyncio.Task | None = None
    if bus is not None:
        bus_q = asyncio.Queue()
        forwarder_task = asyncio.create_task(_bus_forwarder(bus_q, bus, task_id))
    if bus_q is not None or on_event is not None:
        def _observe(event) -> None:
            if bus_q is not None:
                bus_q.put_nowait(event)
            if on_event is not None:
                on_event(event)

        log.on_append(_observe)

    result: RunResult | None = None
    run_error: BaseException | None = None
    worktree_created = False
    worktree_exit_reason = "normal"
    wt = WorktreeSession(task.repo_path, wt_name, engine, task_id)

    try:
        # 显式创建而不使用 async context manager：上下文管理器固定回滚，
        # 无法表达“检测到成果后保留 worktree”的结果策略。
        await wt.create()
        worktree_created = True
        log.log_worktree_created(
            task_id,
            wt.path.name,
            str(wt.path),
            base=wt.base_commit or "HEAD",
        )
        if bus is not None:
            await bus.publish(
                "worktree.created", sender="orchestrator",
                content={
                    "task_id": task_id,
                    "path": str(wt.path),
                    "branch": wt.branch,
                    "base": wt.base_commit,
                },
            )

        # runtime：sandbox 模式把 worktree 以 rw 挂载到 /workspace。
        runtime: Runtime
        if sandbox:
            from tools.runtime import SANDBOX_IMAGE
            git_dir = Path(task.repo_path).resolve() / ".git"
            git_mounts = (
                [(str(git_dir), str(git_dir))]
                if git_dir.is_dir()
                else []
            )
            # 容器看不到宿主文件系统，需要显式挂载 worktree 和独立的
            # .git 目录；readonly_root 只限制容器根文件系统。
            runtime = DockerRuntime(
                repo_path=task.repo_path,
                image=sandbox_image or SANDBOX_IMAGE,
                extra_mounts=git_mounts,
                readonly_root=readonly_root,
                worktree_mount=(str(wt.path), CONTAINER_WORKDIR),
                network=False,
            )
        else:
            # LocalRuntime 在宿主运行，命令的 cwd 直接指向 wt.path，
            # 不需要额外的路径映射或挂载。
            runtime = LocalRuntime()

        try:
            if sandbox:
                # 在调用模型前确认镜像工具和 worktree 可见性。
                preflight = runtime.preflight(cwd=str(wt.path))
                if not preflight.success:
                    detail = preflight.output.strip() or "unknown sandbox error"
                    raise SandboxPreflightError(detail)

            # registry 在 worktree 内执行，PermissionManager 再强制 safe_path 边界。
            registry = registry_builder(
                agent_cfg, confirm_callback, runtime,
                default_cwd=wt.path, workspace=wt.path,
            )
            permission = PermissionManager(workspace=str(wt.path))

            # 每次权限决策都写入 EventLog，便于审计与回放。
            def _on_decision(
                name: str, params: dict[str, Any], decision: PermissionDecision,
            ) -> None:
                log.log_permission_decision(
                    task_id, name, decision.decision.value, decision.reason, params,
                )

            executor = ToolExecutor(
                registry, hooks=agent_cfg.hooks, permission=permission,
                confirm_callback=confirm_callback,
                decision_callback=_on_decision,
            )

            # 创建新 Task 而不修改原对象，让 Agent 的所有相对路径都落在 worktree。
            agent = factory(backend, registry, agent_cfg, executor)
            task_in_wt = dataclasses.replace(task, repo_path=str(wt.path))

            # Agent.run() 保持同步；EventLog 事件先入队，随后由 forwarder 转发。
            result = agent.run(task_in_wt, log)
        finally:
            # runtime 生命周期与 worktree 成果保留策略彼此独立。
            runtime.cleanup()

    except SandboxPreflightError as exc:
        worktree_exit_reason = "preflight_failed"
        reason = f"Sandbox preflight failed: {exc}"
        logger.error(reason)
        log.log_task_failed(steps=0, reason=reason)
        result = RunResult(
            task_id=task_id,
            status=RunStatus.FAILED,
            summary=reason,
            steps_taken=0,
            error=reason,
        )
    except BaseException as exc:  # noqa: BLE001 — 含 KeyboardInterrupt
        run_error = exc

    # bind_worktree 或 created 事件失败时，Git 创建可能已成功但 create() 尚未返回。
    worktree_created = worktree_created or wt.created
    artifact: WorktreeArtifact | None = None
    if worktree_created:
        try:
            partial = run_error is not None or result is None or not result.is_success()
            artifact = await _finalize_worktree(wt, policy, partial=partial)
            final_reason = "exception" if run_error is not None else worktree_exit_reason
            await _record_worktree_finalized(
                log=log,
                bus=bus,
                task_id=task_id,
                name=wt_name,
                artifact=artifact,
                reason=final_reason,
            )
        except BaseException as exc:  # noqa: BLE001 — 最终处置失败不可静默
            if run_error is None:
                run_error = exc
            else:
                logger.exception(
                    "[orchestrate] worktree finalization failed while preserving "
                    "the original exception"
                )

    if run_error is not None:
        try:
            engine.fail_task(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[orchestrate] fail_task on exception path: %s", exc)
        log.log_task_failed(steps=0, reason=f"exception: {run_error!r}")
        log.close()
        await _stop_forwarder(bus_q, forwarder_task)
        raise run_error.with_traceback(run_error.__traceback__)

    if result is None:
        # 防御性保护：正常路径必须产生 RunResult。
        engine.fail_task(task_id)
        log.log_task_failed(steps=0, reason="orchestrator produced no result")
        log.close()
        await _stop_forwarder(bus_q, forwarder_task)
        raise RuntimeError("orchestrator produced no result")

    # CLI/API 通过这个结构化对象拿到保留路径、分支和变更统计。
    result.worktree = artifact

    # Agent 任务状态和 worktree 最终状态分别记账。
    if result.is_success():
        engine.complete_task(task_id)
        if bus is not None:
            await bus.publish(
                "tasks.completed", sender="orchestrator",
                content={"task_id": task_id, "result": result.to_dict()},
            )
    else:
        engine.fail_task(task_id)
        if bus is not None:
            await bus.publish(
                "tasks.failed", sender="orchestrator",
                content={"task_id": task_id, "result": result.to_dict()},
            )

    log.close()
    await _stop_forwarder(bus_q, forwarder_task)
    return result
