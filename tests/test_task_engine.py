"""
tests/test_task_engine.py

M1 Task 1.1：SQLite(WAL) TaskEngine 测试。

用 tmp_path 给每个测试一个独立临时 db；内存库场景用 ":memory:"。
覆盖 s20 移植过来的语义：创建/查询、DAG 依赖判定、原子认领、级联解锁，
以及 M4 要求的并发读写一致性（线程并发 claim）。
"""

from __future__ import annotations

import threading

import pytest

from task.engine import (
    REASON_BLOCKED,
    REASON_WRONG_STATUS,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    Task,
    TaskEngine,
    TaskNotClaimable,
    TaskNotCompletable,
    TaskNotFound,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    """每个测试一个独立临时 db 文件。"""
    e = TaskEngine(tmp_path / "tasks.db")
    yield e
    e.close()


@pytest.fixture
def mem_engine():
    """内存库，最快。"""
    e = TaskEngine(":memory:")
    yield e
    e.close()


# ===========================================================================
# 初始化与建表
# ===========================================================================

class TestInit:
    def test_memory_engine_works(self, mem_engine):
        tid = mem_engine.create_task("hello")
        assert mem_engine.get_task(tid).subject == "hello"

    def test_wal_mode_enabled(self, engine):
        mode = engine._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_db_file_created_with_parent_dir(self, tmp_path):
        db = tmp_path / "nested" / "deep" / "tasks.db"
        e = TaskEngine(db)
        assert db.exists()
        e.close()


# ===========================================================================
# 创建 / 查询
# ===========================================================================

class TestCreateGet:
    def test_create_returns_id_and_defaults(self, engine):
        tid = engine.create_task("subject", "desc")
        assert tid.startswith("task_")
        t = engine.get_task(tid)
        assert t.subject == "subject"
        assert t.description == "desc"
        assert t.status == STATUS_PENDING
        assert t.owner is None
        assert t.worktree is None
        assert isinstance(t.created_at, float)
        assert isinstance(t, Task)

    def test_get_unknown_raises(self, engine):
        with pytest.raises(TaskNotFound):
            engine.get_task("task_nope")

    def test_list_tasks_all_and_by_status(self, engine):
        a = engine.create_task("a")
        b = engine.create_task("b")
        engine.claim_task(a)  # a -> in_progress
        assert {t.id for t in engine.list_tasks()} == {a, b}
        pending = engine.list_tasks(STATUS_PENDING)
        assert [t.id for t in pending] == [b]
        assert engine.list_tasks(STATUS_IN_PROGRESS)[0].id == a


# ===========================================================================
# DAG 依赖判定（can_start）
# ===========================================================================

class TestDependencies:
    def test_no_deps_can_start(self, engine):
        a = engine.create_task("a")
        assert engine.can_start(a) is True

    def test_blocked_until_dep_completed(self, engine):
        b = engine.create_task("b")
        a = engine.create_task("a", blocked_by=[b])
        assert engine.dependencies(a) == [b]
        assert engine.can_start(a) is False  # b 未完成

        engine.claim_task(b)
        engine.complete_task(b)
        assert engine.can_start(a) is True

    def test_add_dependency_after_create(self, engine):
        b = engine.create_task("b")
        a = engine.create_task("a")
        engine.add_dependency(a, b)
        assert engine.can_start(a) is False
        assert engine.dependencies(a) == [b]

    def test_can_start_unknown_raises(self, engine):
        with pytest.raises(TaskNotFound):
            engine.can_start("task_nope")

    def test_chain_dependency(self, engine):
        c = engine.create_task("c")
        b = engine.create_task("b", blocked_by=[c])
        a = engine.create_task("a", blocked_by=[b])
        # a -> b -> c，c 没完成，a/b 都不能启动
        assert engine.can_start(a) is False
        assert engine.can_start(b) is False
        engine.claim_task(c)
        engine.complete_task(c)
        assert engine.can_start(b) is True
        assert engine.can_start(a) is False  # b 还没完成


# ===========================================================================
# claim_task：原子认领 + 异常
# ===========================================================================

class TestClaim:
    def test_claim_success(self, engine):
        a = engine.create_task("a")
        assert engine.claim_task(a, owner="worker-1") is True
        t = engine.get_task(a)
        assert t.status == STATUS_IN_PROGRESS
        assert t.owner == "worker-1"

    def test_claim_blocked_raises(self, engine):
        b = engine.create_task("b")
        a = engine.create_task("a", blocked_by=[b])
        with pytest.raises(TaskNotClaimable) as exc:
            engine.claim_task(a)
        assert exc.value.reason == REASON_BLOCKED
        assert exc.value.blocked_by == [b]

    def test_reclaim_in_progress_raises_wrong_status(self, engine):
        # 认领后任务进入 in_progress，再次认领应失败。
        # 语义对齐 s20：按「状态不对」报错（任务已不是 pending），
        # 而非 already_owned——后者只用于 pending+owner 非空的异常态。
        a = engine.create_task("a")
        assert engine.claim_task(a, owner="w1") is True
        with pytest.raises(TaskNotClaimable) as exc:
            engine.claim_task(a, owner="w2")
        assert exc.value.reason == REASON_WRONG_STATUS
        # 原所有者不变
        assert engine.get_task(a).owner == "w1"

    def test_claim_completed_raises_wrong_status(self, engine):
        a = engine.create_task("a")
        engine.claim_task(a)
        engine.complete_task(a)
        with pytest.raises(TaskNotClaimable) as exc:
            engine.claim_task(a)
        assert exc.value.reason == REASON_WRONG_STATUS

    def test_claim_unknown_raises(self, engine):
        with pytest.raises(TaskNotFound):
            engine.claim_task("task_nope")


# ===========================================================================
# complete_task：级联解锁
# ===========================================================================

class TestComplete:
    def test_complete_success(self, engine):
        a = engine.create_task("a")
        engine.claim_task(a)
        unblocked = engine.complete_task(a)
        assert unblocked == []
        assert engine.get_task(a).status == STATUS_COMPLETED

    def test_complete_cascades_unblock(self, engine):
        b = engine.create_task("b")
        a = engine.create_task("a", blocked_by=[b])
        engine.claim_task(b)
        unblocked = engine.complete_task(b)
        assert a in unblocked
        assert engine.can_start(a) is True

    def test_complete_not_in_progress_raises(self, engine):
        a = engine.create_task("a")
        with pytest.raises(TaskNotCompletable):
            engine.complete_task(a)

    def test_double_complete_raises(self, engine):
        a = engine.create_task("a")
        engine.claim_task(a)
        engine.complete_task(a)
        with pytest.raises(TaskNotCompletable):
            engine.complete_task(a)


# ===========================================================================
# fail_task / bind_worktree
# ===========================================================================

class TestFailBind:
    def test_fail_task(self, engine):
        a = engine.create_task("a")
        engine.claim_task(a)
        engine.fail_task(a)
        assert engine.get_task(a).status == "failed"

    def test_bind_worktree(self, engine):
        a = engine.create_task("a")
        engine.bind_worktree(a, "wt/feature-a")
        assert engine.get_task(a).worktree == "wt/feature-a"

    def test_bind_unknown_raises(self, engine):
        with pytest.raises(TaskNotFound):
            engine.bind_worktree("task_nope", "wt/x")


# ===========================================================================
# 并发原子性（M4 要求的并发读写一致性提前覆盖）
# ===========================================================================

class TestConcurrency:
    def test_concurrent_claim_exactly_one_winner(self, tmp_path):
        """
        多个独立连接（模拟多个 agent）同时 claim 同一 pending 任务，
        断言恰好一个成功、其余得到 already_owned。

        这是「多连接共享同一 WAL db 文件」的并发模型——SQLite 在 DB 层用
        写锁保证 UPDATE...WHERE status='pending' AND owner IS NULL 的原子性，
        消除 s20 版 load→改→save 之间的竞态。注意：单连接不能跨线程并发
        execute（会 InterfaceError），所以每个 worker 用自己的 TaskEngine 实例。
        """
        db = tmp_path / "race.db"
        setup = TaskEngine(db)
        tid = setup.create_task("race")
        setup.close()

        winners: list[bool] = []
        reasons: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def worker(name: str):
            # 每个 worker 独立连接同一 db 文件
            e = TaskEngine(db)
            try:
                start.wait()  # 尽量同时开跑
                ok = e.claim_task(tid, owner=name)
                with lock:
                    winners.append(ok)
            except TaskNotClaimable as exc:
                with lock:
                    reasons.append(exc.reason)
            finally:
                e.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert winners.count(True) == 1, f"应有且仅有一个赢家，实际 {winners}"
        assert len(winners) + len(reasons) == 8

        # 用一个新连接核验最终状态
        check = TaskEngine(db)
        assert check.get_task(tid).status == STATUS_IN_PROGRESS
        assert check.get_task(tid).owner is not None
        check.close()
