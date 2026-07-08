"""
tests/test_orchestrate.py

M4 主循环集成测试（第一波）。

orchestrate_run 是 async 组合根，把 TaskEngine + WorktreeSession + Permission
( workspace 边界) + ToolExecutor(决策回调) + AgentBus 接进同步 ReAct 循环。
用 tmp_path + git fixture 造隔离 repo，FakeAgent 替换真 Agent 跑出确定性结果。

覆盖：
- 成功路径：worktree 创建/清理、task=completed、JSONL 事件顺序
- 失败回滚：FakeAgent 抛异常 → worktree 清理、task=failed、reason=exception
- 权限拒绝记事件：越界写 → permission_decision(deny)，run 不崩
- AgentBus 广播生命周期事件
- bus=None 正常工作
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from agent.event_log import EventLog
from agent.orchestrate import orchestrate_run
from agent.task import RunResult, RunStatus, Task
from agent.core import AgentConfig
from ipc.bus import AgentBus
from task.engine import STATUS_COMPLETED, STATUS_FAILED, TaskEngine
from tools.base import ToolRegistry
from tools.file_tool import FileWriteTool


# ---------------------------------------------------------------------------
# fixtures（仿 test_worktree_session.py）
# ---------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.com"], r)
    _git(["config", "user.name", "T"], r)
    (r / "README.md").write_text("# repo")
    _git(["add", "."], r)
    _git(["commit", "-q", "-m", "init"], r)
    return r


@pytest.fixture(autouse=True)
def fake_worktree_session(monkeypatch):
    """orchestrate 单测聚焦编排逻辑；真实 git worktree 生命周期由 test_worktree_session 覆盖。"""
    import agent.orchestrate as orch_mod

    class FakeWorktreeSession:
        def __init__(self, repo_path, name, task_engine=None, task_id=None, **kwargs):
            self.repo_path = Path(repo_path)
            self.name = name
            self.task_engine = task_engine
            self.task_id = task_id
            self.path = self.repo_path / ".worktrees" / name

        async def __aenter__(self):
            self.path.mkdir(parents=True, exist_ok=True)
            if self.task_engine is not None and self.task_id is not None:
                self.task_engine.bind_worktree(self.task_id, self.name)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            shutil.rmtree(self.path, ignore_errors=True)

    monkeypatch.setattr(orch_mod, "WorktreeSession", FakeWorktreeSession)


def _build_registry(cfg, confirm_callback, runtime, worktree_path):
    """最小 registry：只含 file_write（够覆盖权限/写入场景）。"""
    return ToolRegistry().register(FileWriteTool())


class FakeAgent:
    """替身：不走 LLM，按预设 result 返回。记录 task 以便断言 repo_path。"""
    def __init__(self, backend, registry, config, executor):
        self.reg = registry
        self.exe = executor
        self.runs = []

    def run(self, task, log):
        self.runs.append(task)
        return RunResult(
            task_id=task.task_id, status=RunStatus.SUCCESS,
            summary="fake done", steps_taken=1,
        )


class FailingAgent:
    """替身：run 时抛异常，触发异常回滚路径。"""
    def __init__(self, backend, registry, config, executor):
        self.exe = executor

    def run(self, task, log):
        raise RuntimeError("agent crashed mid-run")


class InterruptedAgent:
    """替身：run 时抛 KeyboardInterrupt，模拟用户中断 / 崩溃。"""
    def __init__(self, backend, registry, config, executor):
        pass

    def run(self, task, log):
        raise KeyboardInterrupt


class MaxStepsAgent:
    """替身：返回 max_steps 失败 result（非异常，但任务未成功）。"""
    def __init__(self, backend, registry, config, executor):
        pass

    def run(self, task, log):
        return RunResult(
            task_id=task.task_id, status=RunStatus.MAX_STEPS,
            summary="hit step limit", steps_taken=task.max_steps,
        )


class WritingAgent:
    """替身：在 worktree 内写文件（走 executor/permission 校验），再成功。
    越界写则用 path 逃逸触发 DENY，验证 run 不崩。"""
    def __init__(self, backend, registry, config, executor):
        self.exe = executor
        self.denied = False

    def run(self, task, log):
        log.log_task_start(task)
        # 先尝试越界写（应被 permission DENY，返回 error，不抛异常）
        bad = self.exe.execute("file_write",
                               {"path": "/etc/forbidden_m4", "content": "x"})
        if not bad.success:
            self.denied = True
        # 再合法写 worktree 内文件
        self.exe.execute("file_write",
                         {"path": str(Path(task.repo_path) / "ok.txt"),
                          "content": "hi"})
        log.log_task_complete(steps=1, summary="done")
        return RunResult(task_id=task.task_id, status=RunStatus.SUCCESS,
                         summary="done", steps_taken=1)


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------

class TestOrchestrateSuccess:
    async def test_success_completes_and_cleans(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="ok", repo_path=str(repo)),
            engine=engine,
            registry_builder=_build_registry,
            log_dir=str(tmp_path / "logs"),
            config=AgentConfig(),
            agent_factory=FakeAgent,
        )
        assert result.is_success()
        assert engine.get_task(result.task_id).status == STATUS_COMPLETED
        # worktree 已清理
        wt_dir = repo / ".worktrees"
        assert not wt_dir.exists() or not any(wt_dir.iterdir())

    async def test_success_event_order(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="ok", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            log_dir=str(tmp_path / "logs"),
            agent_factory=FakeAgent,
        )
        log_file = next((tmp_path / "logs").glob("*.jsonl"))
        types = [e.event_type.value for e in EventLog.open_existing(log_file).replay()]
        # worktree_created 必须在 worktree_removed 之前
        assert types.index("worktree_created") < types.index("worktree_removed")
        # task_claimed 在最前
        assert types[0] == "task_claimed"

    async def test_sandbox_runtime_cleanup_on_success(self, repo, tmp_path, monkeypatch):
        """sandbox=True 时 DockerRuntime 即使由 orchestrator 创建，也必须释放。"""
        import agent.orchestrate as orch_mod
        from tools.runtime import LocalRuntime

        instances = []

        class FakeDockerRuntime(LocalRuntime):
            def __init__(self, *args, **kwargs):
                self.cleaned = False
                self.args = args
                self.kwargs = kwargs
                instances.append(self)

            @property
            def name(self):
                return "fake-docker"

            def cleanup(self):
                self.cleaned = True

        monkeypatch.setattr(orch_mod, "DockerRuntime", FakeDockerRuntime)

        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="ok", repo_path=str(repo)),
            engine=engine,
            registry_builder=_build_registry,
            log_dir=str(tmp_path / "logs"),
            sandbox=True,
            agent_factory=FakeAgent,
        )

        assert result.is_success()
        assert instances and instances[0].cleaned is True
        assert instances[0].kwargs["repo_path"] == str(repo)
        assert instances[0].kwargs["worktree_mount"][1] == "/workspace"


# ---------------------------------------------------------------------------
# 失败回滚
# ---------------------------------------------------------------------------

class TestOrchestrateRollback:
    async def test_exception_cleans_and_fails(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        with pytest.raises(RuntimeError, match="crashed"):
            await orchestrate_run(
                backend=None,
                task=Task(description="boom", repo_path=str(repo)),
                engine=engine, registry_builder=_build_registry,
                log_dir=str(tmp_path / "logs"),
                agent_factory=FailingAgent,
            )
        # 最后一个 task 应是 failed（按创建时间取最新）
        # 直接通过 engine 的内部查询更稳：
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "tasks.db"))
        statuses = [r[0] for r in conn.execute("SELECT status FROM tasks").fetchall()]
        conn.close()
        assert STATUS_FAILED in statuses
        # worktree 已清理
        wt_dir = repo / ".worktrees"
        assert not wt_dir.exists() or not any(wt_dir.iterdir())

    async def test_sandbox_runtime_cleanup_on_exception(self, repo, tmp_path, monkeypatch):
        import agent.orchestrate as orch_mod
        from tools.runtime import LocalRuntime

        instances = []

        class FakeDockerRuntime(LocalRuntime):
            def __init__(self, *args, **kwargs):
                self.cleaned = False
                instances.append(self)

            @property
            def name(self):
                return "fake-docker"

            def cleanup(self):
                self.cleaned = True

        monkeypatch.setattr(orch_mod, "DockerRuntime", FakeDockerRuntime)

        engine = TaskEngine(tmp_path / "tasks.db")
        with pytest.raises(RuntimeError):
            await orchestrate_run(
                backend=None,
                task=Task(description="boom", repo_path=str(repo)),
                engine=engine,
                registry_builder=_build_registry,
                log_dir=str(tmp_path / "logs"),
                sandbox=True,
                agent_factory=FailingAgent,
            )

        assert instances and instances[0].cleaned is True

    async def test_exception_logs_worktree_removed_reason(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        with pytest.raises(RuntimeError):
            await orchestrate_run(
                backend=None,
                task=Task(description="boom", repo_path=str(repo)),
                engine=engine, registry_builder=_build_registry,
                log_dir=str(tmp_path / "logs"),
                agent_factory=FailingAgent,
            )
        log_file = next((tmp_path / "logs").glob("*.jsonl"))
        removes = [e for e in EventLog.open_existing(log_file).replay()
                   if e.event_type.value == "worktree_removed"]
        assert removes, "应记 worktree_removed 事件"
        assert removes[-1].payload["reason"] == "exception"

    async def test_keyboard_interrupt_cleans_and_fails(self, repo, tmp_path):
        """KeyboardInterrupt（用户中断/崩溃）→ worktree 清理 + task=failed。"""
        import sqlite3
        engine = TaskEngine(tmp_path / "tasks.db")
        with pytest.raises(KeyboardInterrupt):
            await orchestrate_run(
                backend=None,
                task=Task(description="interrupted", repo_path=str(repo)),
                engine=engine, registry_builder=_build_registry,
                log_dir=str(tmp_path / "logs"),
                agent_factory=InterruptedAgent,
            )
        # task 状态 = failed
        conn = sqlite3.connect(str(tmp_path / "tasks.db"))
        statuses = [r[0] for r in conn.execute("SELECT status FROM tasks").fetchall()]
        conn.close()
        assert STATUS_FAILED in statuses
        # worktree 已清理（事务回滚）
        wt_dir = repo / ".worktrees"
        assert not wt_dir.exists() or not any(wt_dir.iterdir())
        # JSONL 记了异常 reason
        log_file = next((tmp_path / "logs").glob("*.jsonl"))
        removes = [e for e in EventLog.open_existing(log_file).replay()
                   if e.event_type.value == "worktree_removed"]
        assert removes and removes[-1].payload["reason"] == "exception"

    async def test_keyboard_interrupt_broadcasts_on_bus(self, repo, tmp_path):
        """中断时 bus 广播 worktree.removed(reason=exception)。"""
        import asyncio
        engine = TaskEngine(tmp_path / "tasks.db")
        bus = AgentBus()
        q = bus.subscribe("worktree.*")
        with pytest.raises(KeyboardInterrupt):
            await orchestrate_run(
                backend=None,
                task=Task(description="bus-interrupt", repo_path=str(repo)),
                engine=engine, registry_builder=_build_registry,
                bus=bus, log_dir=str(tmp_path / "logs"),
                agent_factory=InterruptedAgent,
            )
        await asyncio.sleep(0.05)
        received: list[str] = []
        while not q.empty():
            received.append(q.get_nowait().content.get("reason"))
        assert "exception" in received

    async def test_failed_result_marks_task_failed(self, repo, tmp_path):
        """agent 返回 max_steps（非异常）→ task=failed，result 正常返回。"""
        import sqlite3
        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="maxsteps", repo_path=str(repo), max_steps=3),
            engine=engine, registry_builder=_build_registry,
            log_dir=str(tmp_path / "logs"),
            agent_factory=MaxStepsAgent,
        )
        # result 正常返回（非抛异常），但非成功
        assert not result.is_success()
        assert result.status == RunStatus.MAX_STEPS
        # task 状态 = failed
        conn = sqlite3.connect(str(tmp_path / "tasks.db"))
        statuses = [r[0] for r in conn.execute("SELECT status FROM tasks").fetchall()]
        conn.close()
        assert STATUS_FAILED in statuses
        # worktree_removed 记 normal（不是 exception，因为是正常返回）
        log_file = next((tmp_path / "logs").glob("*.jsonl"))
        removes = [e for e in EventLog.open_existing(log_file).replay()
                   if e.event_type.value == "worktree_removed"]
        assert removes and removes[-1].payload["reason"] == "normal"


# ---------------------------------------------------------------------------
# 权限拒绝记事件（workspace 边界）
# ---------------------------------------------------------------------------

class TestOrchestratePermission:
    async def test_permission_deny_recorded_and_run_survives(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="write", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            log_dir=str(tmp_path / "logs"),
            agent_factory=WritingAgent,
        )
        assert result.is_success()   # DENY 不应让 run 崩
        log_file = next((tmp_path / "logs").glob("*.jsonl"))
        decisions = [
            e for e in EventLog.open_existing(log_file).replay()
            if e.event_type.value == "permission_decision"
        ]
        denied = [d for d in decisions if d.payload["decision"] == "deny"]
        assert denied, "越界写应有 deny 决策事件"
        assert denied[0].payload["tool"] == "file_write"


# ---------------------------------------------------------------------------
# AgentBus
# ---------------------------------------------------------------------------

class TestOrchestrateBus:
    async def test_bus_publishes_lifecycle(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        bus = AgentBus()

        import asyncio
        task_q = bus.subscribe("tasks.*")
        wt_q = bus.subscribe("worktree.*")
        await orchestrate_run(
            backend=None,
            task=Task(description="bus", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            bus=bus, log_dir=str(tmp_path / "logs"),
            agent_factory=FakeAgent,
        )
        await asyncio.sleep(0)
        received = []
        while not task_q.empty():
            received.append(task_q.get_nowait().topic)
        while not wt_q.empty():
            received.append(wt_q.get_nowait().topic)
        assert "tasks.claimed" in received or "tasks.completed" in received
        assert "worktree.created" in received

    async def test_no_bus_works(self, repo, tmp_path):
        engine = TaskEngine(tmp_path / "tasks.db")
        result = await orchestrate_run(
            backend=None,
            task=Task(description="nobus", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            bus=None, log_dir=str(tmp_path / "logs"),
            agent_factory=FakeAgent,
        )
        assert result.is_success()

    async def test_bus_forwards_step_events(self, repo, tmp_path):
        """M4 第二波：on_append→forwarder 把每条 event 转发到 events.* 。"""
        import asyncio
        engine = TaskEngine(tmp_path / "tasks.db")
        bus = AgentBus()

        event_types: list[str] = []

        async def collect():
            async for msg in bus.messages("events.*"):
                event_types.append(msg.content["event_type"])

        c = asyncio.create_task(collect())
        result = await orchestrate_run(
            backend=None,
            task=Task(description="forward", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            bus=bus, log_dir=str(tmp_path / "logs"),
            agent_factory=WritingAgent,   # 会触发 task_start + permission_decision + task_complete
        )
        # forwarder 在 cleanup 前会收到哨兵终止；给收集任务一点时间
        await asyncio.sleep(0.05)
        c.cancel()
        assert result.is_success()
        assert "task_start" in event_types
        assert "permission_decision" in event_types
        assert "task_complete" in event_types


# ---------------------------------------------------------------------------
# forwarder 健壮性（单元，直接测内部协程）
# ---------------------------------------------------------------------------

class TestBusForwarderRobustness:
    async def test_forwarder_survives_bus_publish_error(self):
        """bus.publish 抛异常时 forwarder 不崩，继续处理后续事件。"""
        import asyncio
        from agent.orchestrate import _bus_forwarder

        class BrokenBus:
            publish_count = 0

            async def publish(self, *args, **kwargs):
                self.publish_count += 1
                raise RuntimeError("bus down")

        q: asyncio.Queue = asyncio.Queue()
        broken = BrokenBus()
        task = asyncio.create_task(_bus_forwarder(q, broken, "t1"))   # type: ignore[arg-type]

        # 喂两条事件 + 哨兵
        from agent.task import Event, EventType
        q.put_nowait(Event(EventType.TASK_CLAIMED, "t1", {"x": 1}))
        q.put_nowait(Event(EventType.ACTION, "t1", {"y": 2}))
        q.put_nowait(None)

        await asyncio.wait_for(task, timeout=1.0)   # 正常退出（没卡、没抛）
        assert broken.publish_count == 2   # 两条都尝试过

    async def test_stop_forwarder_noop_on_none(self):
        """_stop_forwarder(None, None) 空操作，不报错。"""
        import asyncio
        from agent.orchestrate import _stop_forwarder
        await _stop_forwarder(None, None)   # 不应抛异常

    async def test_stop_forwarder_cancels_stuck_task(self):
        """forwarder 卡住（不退）时，_stop_forwarder 超时后取消它。"""
        import asyncio
        from agent.orchestrate import _bus_forwarder, _stop_forwarder

        q: asyncio.Queue = asyncio.Queue()
        # 不喂哨兵 → forwarder 永远 await q.get()，_stop_forwarder 会走 timeout 分支
        stuck = asyncio.create_task(_bus_forwarder(q, AgentBus(), "t2"))
        await asyncio.sleep(0.05)   # 确保 forwarder 已在 await

        # _stop_forwarder 会 put 哨兵让 forwarder 正常退出；这里验证它能干净结束
        await asyncio.wait_for(_stop_forwarder(q, stuck), timeout=2.0)
        assert stuck.done()
