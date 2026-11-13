"""
tests/test_task_engine_concurrency.py

任务规划 Task 4.1：SQLite(WAL) TaskEngine 并发读写一致性测试。

设计说明（重要）：
TaskEngine 使用 SQLite WAL 保障多连接并发一致性，同时在单实例内用 RLock
串行化同一 sqlite3 connection 的访问。测试同时覆盖多连接和共享实例两种形态。

验证：
- 多连接并发 claim 同一 pending 任务：恰好一个成功（UPDATE...WHERE 原子性）
- 共享 TaskEngine 实例并发 claim：不触发 sqlite driver 竞态，仍恰好一个成功
- 多连接并发 claim 不同任务：互不干扰
- blocked 任务的并发 claim：一致地 TaskNotClaimable(BLOCKED)
- complete 后的终态：并发 claim 不会复活
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from task.engine import (
    REASON_ALREADY_OWNED,
    REASON_BLOCKED,
    REASON_WRONG_STATUS,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    TaskEngine,
    TaskNotClaimable,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "tasks.db")


# ---------------------------------------------------------------------------
# 并发 claim 同一任务（多连接）
# ---------------------------------------------------------------------------

class TestConcurrentClaim:
    def test_concurrent_claim_same_task_single_winner(self, db_path):
        """N 连接同时 claim 同一 pending → 恰好 1 成功，其余 ALREADY_OWNED/WRONG。"""
        setup = TaskEngine(db_path)
        tid = setup.create_task("race")
        setup.close()

        N = 8
        results: list[bool | str] = []
        lock = threading.Lock()
        start = threading.Barrier(N)

        def claim():
            eng = TaskEngine(db_path)   # 每线程独立连接（WAL）
            try:
                start.wait()
                ok = eng.claim_task(tid, owner=f"w-{threading.get_ident()}")
                with lock:
                    results.append(ok)
            except TaskNotClaimable as e:
                with lock:
                    results.append(e.reason)
            finally:
                eng.close()

        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(lambda _: claim(), range(N)))

        successes = sum(1 for r in results if r is True)
        assert successes == 1, f"恰好一个认领成功，实际 {successes}"

        verify = TaskEngine(db_path)
        t = verify.get_task(tid)
        verify.close()
        assert t.status == STATUS_IN_PROGRESS
        assert t.owner is not None

    def test_shared_engine_concurrent_claim_same_task_single_winner(self, db_path):
        """共享同一个 TaskEngine 实例并发 claim，也应由实例锁串行化。"""
        engine = TaskEngine(db_path)
        tid = engine.create_task("shared-race")

        N = 8
        results: list[bool | str] = []
        lock = threading.Lock()
        start = threading.Barrier(N)

        def claim():
            try:
                start.wait()
                ok = engine.claim_task(tid, owner=f"w-{threading.get_ident()}")
                with lock:
                    results.append(ok)
            except TaskNotClaimable as e:
                with lock:
                    results.append(e.reason)

        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(lambda _: claim(), range(N)))

        successes = sum(1 for r in results if r is True)
        assert successes == 1, results
        assert engine.get_task(tid).status == STATUS_IN_PROGRESS
        engine.close()

    def test_concurrent_claim_different_tasks_all_succeed(self, db_path):
        """并发 claim 不同任务：互不干扰，全部成功。"""
        setup = TaskEngine(db_path)
        tids = [setup.create_task(f"t{i}") for i in range(6)]
        setup.close()

        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(len(tids))

        def claim(tid):
            eng = TaskEngine(db_path)
            try:
                start.wait()
                ok = eng.claim_task(tid, owner=f"w-{tid}")
                with lock:
                    results.append(ok)
            finally:
                eng.close()

        with ThreadPoolExecutor(max_workers=len(tids)) as ex:
            ex.map(claim, tids)

        assert all(results)
        verify = TaskEngine(db_path)
        for tid in tids:
            assert verify.get_task(tid).status == STATUS_IN_PROGRESS
        verify.close()

    def test_blocked_task_claim_fails_consistently(self, db_path):
        """依赖未完成的任务：并发 claim 一致地 BLOCKED。"""
        setup = TaskEngine(db_path)
        parent = setup.create_task("parent")
        child = setup.create_task("child")
        setup.add_dependency(child, parent)
        setup.close()

        reasons: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(5)

        def claim():
            eng = TaskEngine(db_path)
            try:
                start.wait()
                eng.claim_task(child)
            except TaskNotClaimable as e:
                with lock:
                    reasons.append(e.reason)
            finally:
                eng.close()

        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(lambda _: claim(), range(5)))

        assert reasons and all(r == REASON_BLOCKED for r in reasons), reasons

        # parent 完成后 child 解锁
        fix = TaskEngine(db_path)
        fix.claim_task(parent)
        fix.complete_task(parent)
        assert fix.can_start(child)
        fix.close()


# ---------------------------------------------------------------------------
# 终态一致性
# ---------------------------------------------------------------------------

class TestTerminalStateConsistency:
    def test_complete_is_terminal_under_concurrency(self, db_path):
        """complete 后并发 claim 不会复活。"""
        setup = TaskEngine(db_path)
        tid = setup.create_task("t")
        setup.claim_task(tid)
        setup.complete_task(tid)
        setup.close()

        reasons: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def claim():
            eng = TaskEngine(db_path)
            try:
                start.wait()
                eng.claim_task(tid)
            except TaskNotClaimable as e:
                with lock:
                    reasons.append(e.reason)
            finally:
                eng.close()

        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda _: claim(), range(6)))

        assert all(r in (REASON_WRONG_STATUS, REASON_ALREADY_OWNED) for r in reasons), reasons
        verify = TaskEngine(db_path)
        assert verify.get_task(tid).status == STATUS_COMPLETED
        verify.close()

    def test_create_generates_uuid_id_format(self, db_path):
        """create 产生 task_{uuid_hex} 格式的 id，可回查。"""
        eng = TaskEngine(db_path)
        tid = eng.create_task("solo")
        eng.close()

        assert tid.startswith("task_")
        parts = tid.split("_")
        assert len(parts) == 2 and len(parts[1]) == 32
        int(parts[1], 16)

        verify = TaskEngine(db_path)
        assert verify.get_task(tid).status == STATUS_PENDING
        verify.close()

    def test_high_volume_create_ids_do_not_collide(self, db_path):
        """UUID task id 在高频创建时不应撞 UNIQUE。"""
        eng = TaskEngine(db_path)
        tids = [eng.create_task(f"t{i}") for i in range(500)]
        assert len(tids) == len(set(tids))
        assert len(eng.list_tasks()) == 500
        eng.close()
