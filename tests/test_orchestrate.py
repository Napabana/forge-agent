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

        received: list = []
        async def collect():
            async for msg in bus.messages("tasks.*"):
                received.append(msg.topic)
                if "completed" in msg.topic or "failed" in msg.topic:
                    break
            # 也订 worktree（独立队列）
        async def collect_wt():
            async for msg in bus.messages("worktree.*"):
                received.append(msg.topic)
                if "created" in msg.topic:
                    break

        import asyncio
        c1 = asyncio.create_task(collect())
        c2 = asyncio.create_task(collect_wt())
        await orchestrate_run(
            backend=None,
            task=Task(description="bus", repo_path=str(repo)),
            engine=engine, registry_builder=_build_registry,
            bus=bus, log_dir=str(tmp_path / "logs"),
            agent_factory=FakeAgent,
        )
        await asyncio.sleep(0.05)
        c1.cancel(); c2.cancel()
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
