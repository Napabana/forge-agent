"""
agent/orchestrate.py

M4 主循环集成：把 M1（TaskEngine + WorktreeSession）、M2（AgentBus +
ToolExecutor/Permission）、M3（DockerRuntime 加固）接进 ReAct 主循环。

核心设计——同步/异步阻抗失配的解法：
- Agent.run() 是同步 ReAct 循环（清晰可测，CLAUDE.md 约束保持同步）。
- WorktreeSession / AgentBus 是 async。
- 解法：本模块提供一个 async 组合根 orchestrate_run()，用 `async with
  WorktreeSession(...)` 包住整次运行。Agent.run() 同步执行，EventLog 事件
  先进入队列，运行结束后由 forwarder 转发到 bus。Agent.run() 一行不改。

safe_path 注入：PermissionManager(workspace=str(wt.path)) 在 executor 层
强制文件路径不逃逸 worktree（读写工具都覆盖）。不依赖 LLM 填的 params。

事件审计：orchestrator 在 worktree 生命周期 + 权限决策点上写新事件类型
（WORKTREE_CREATED/REMOVED、PERMISSION_DECISION、TASK_CLAIMED）到 EventLog，
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
from runtime.worktree import WorktreeSession
from task.engine import TaskEngine
from tools.runtime import CONTAINER_WORKDIR, DockerRuntime, LocalRuntime, Runtime

logger = logging.getLogger(__name__)


# registry_builder 契约：(config, confirm_callback, runtime, worktree_path) -> ToolRegistry
# 注入是为了避免 agent 层 import entry/cli（拉入 Click 依赖）。
RegistryBuilder = Callable[..., Any]

# agent_factory 契约：(backend, registry, config, executor) -> Agent
# 默认造真 Agent；测试可注入 FakeAgent。
AgentFactory = Callable[..., Agent]


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

    Returns:
        RunResult（agent 的最终结果）
    """
    agent_cfg = config or AgentConfig()
    factory = agent_factory or _default_agent_factory

    # 1. TaskEngine 记账：创建任务 → 认领。TaskEngine 为 task_id 权威源。
    task_id = engine.create_task(
        subject=task.description[:80],
        description=task.description,
    )
    task.task_id = task_id   # 让 EventLog / RunResult 的 task_id 与 DB 一致
    engine.claim_task(task_id, owner="agent")

    # 2. EventLog + bus 生命周期事件
    log = EventLog.create(task, log_dir=log_dir)
    log.log_task_claimed(task_id, owner="agent")
    if bus is not None:
        await bus.publish(
            "tasks.claimed", sender="orchestrator",
            content={"task_id": task_id},
            metadata={"subject": task.description[:80]},
        )

    wt_name = worktree_name or f"task-{task_id}"

    # bus 逐步事件转发（M4 第二波）：on_append → queue → forwarder 协程。
    # 同步循环每写一条 event 就 put_nowait，forwarder 异步 publish 到 events.* 。
    bus_q: asyncio.Queue | None = None
    forwarder_task: asyncio.Task | None = None
    if bus is not None:
        bus_q = asyncio.Queue()
        log.on_append(lambda ev: bus_q.put_nowait(ev))
        forwarder_task = asyncio.create_task(_bus_forwarder(bus_q, bus, task_id))

    result: RunResult | None = None
    exc_info: BaseException | None = None

    try:
        async with WorktreeSession(task.repo_path, wt_name, engine, task_id) as wt:
            log.log_worktree_created(task_id, wt.path.name, str(wt.path))
            if bus is not None:
                await bus.publish(
                    "worktree.created", sender="orchestrator",
                    content={"task_id": task_id, "path": str(wt.path)},
                )

            # 3. runtime（M3：sandbox 模式把 worktree rw 挂 /workspace，只读根）
            runtime: Runtime
            if sandbox:
                from tools.runtime import SANDBOX_IMAGE
                runtime = DockerRuntime(
                    repo_path=task.repo_path,
                    image=sandbox_image or SANDBOX_IMAGE,
                    readonly_root=readonly_root,
                    worktree_mount=(str(wt.path), CONTAINER_WORKDIR),
                    network=False,
                )
            else:
                runtime = LocalRuntime()

            try:
                # 4. registry（在 worktree 内执行）+ permission（safe_path 边界）
                registry = registry_builder(
                    agent_cfg, confirm_callback, runtime, worktree_path=wt.path,
                )
                permission = PermissionManager(workspace=str(wt.path))

                # 权限决策观察回调 → 写 PERMISSION_DECISION + 转发 bus
                def _on_decision(
                    name: str, params: dict[str, Any], decision: PermissionDecision,
                ) -> None:
                    log.log_permission_decision(
                        task_id, name, decision.decision.value, decision.reason, params,
                    )

                executor = ToolExecutor(
                    registry, permission=permission,
                    confirm_callback=confirm_callback,
                    decision_callback=_on_decision,
                )

                # 5. Agent：让它的 repo_path 指向 worktree（core.py 零改动）
                agent = factory(backend, registry, agent_cfg, executor)
                task_in_wt = dataclasses.replace(task, repo_path=str(wt.path))

                # 6. 同步循环。事件通过 EventLog.on_append 先入队，运行结束后
                # forwarder 转发，避免线程调度导致的收尾不确定性。
                result = agent.run(task_in_wt, log)
            finally:
                runtime.cleanup()

    except BaseException as exc:  # noqa: BLE001 — 捕获含 KeyboardInterrupt 的回滚路径
        exc_info = exc
        # WorktreeSession.__aexit__ 已强制清理（事务语义），这里只记账
        log.log_worktree_removed(task_id, wt_name, "", reason="exception")
        if bus is not None:
            await bus.publish(
                "worktree.removed", sender="orchestrator",
                content={"task_id": task_id, "reason": "exception"},
            )
        try:
            engine.fail_task(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[orchestrate] fail_task on exception path: %s", e)
        log.log_task_failed(steps=0, reason=f"exception: {exc!r}")
        log.close()
        await _stop_forwarder(bus_q, forwarder_task)
        raise

    # 7. 正常退出：记账 + 清理意图
    log.log_worktree_removed(task_id, wt_name, "", reason="normal")
    if result is not None and result.is_success():
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
                content={"task_id": task_id,
                         "result": result.to_dict() if result else None},
            )

    log.close()
    await _stop_forwarder(bus_q, forwarder_task)
    return result
