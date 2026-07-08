"""
task/engine.py

SQLite(WAL) 持久化的任务状态机 + DAG 依赖引擎（M1 Task 1.1）。

设计来源：s20_comprehensive/code.py 的 Task System（70-168 行）——
Task dataclass、create/load/list/can_start/claim/complete 一整套语义。
本模块完整保留其业务语义，只把存储层从「每任务一个 JSON 文件」换成 SQLite，
用 UPDATE ... WHERE 的原子性消除 s20 版 load→改→save 之间的并发竞态。

API 风格：结构化 + 异常。
- claim_task 返回 bool / 失败 raise TaskNotClaimable（带 reason）
- complete_task 返回被解锁的任务 id 列表
- 查询返回 Task 对象；不存在 raise TaskNotFound

M1 边界：独立模块，不集成进 agent/core.py 主循环。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# claim 失败原因（TaskNotClaimable.reason）
REASON_ALREADY_OWNED = "already_owned"
REASON_WRONG_STATUS = "wrong_status"
REASON_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """一条任务记录。字段对齐 s20 code.py:79-88，移除 JSON 专属字段，加 created_at。"""
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    worktree: str | None
    created_at: float


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class TaskEngineError(Exception):
    """TaskEngine 所有自定义异常的基类。"""


class TaskNotFound(TaskEngineError):
    """任务 id 不存在。"""


class TaskNotClaimable(TaskEngineError):
    """claim_task 失败。reason 见 REASON_* 常量，blocked 时附 blocked_by 列表。"""

    def __init__(self, task_id: str, reason: str, blocked_by: list[str] | None = None):
        self.task_id = task_id
        self.reason = reason
        self.blocked_by = blocked_by or []
        detail = f"task {task_id!r} not claimable: {reason}"
        if blocked_by:
            detail += f" (blocked by {blocked_by})"
        super().__init__(detail)


class TaskNotCompletable(TaskEngineError):
    """complete_task 失败（任务不在 in_progress 状态）。"""

    def __init__(self, task_id: str, status: str):
        self.task_id = task_id
        self.status = status
        super().__init__(f"task {task_id!r} is {status!r}, cannot complete")


# ---------------------------------------------------------------------------
# TaskEngine
# ---------------------------------------------------------------------------

class TaskEngine:
    """
    SQLite(WAL) 任务状态机。

    Args:
        db_path: SQLite 数据库路径。默认 ".forge/tasks.db"（相对 cwd）。
                 ":memory:" 为内存库（测试用）。
                 父目录不存在时会自动创建。
    """

    def __init__(self, db_path: str | Path = ".forge/tasks.db") -> None:
        self._db_path = str(db_path)

        # 落盘库：确保父目录存在（内存库无需）
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False：允许测试里跨线程访问（WAL + 串行写）
        self._conn = sqlite3.connect(self._db_path, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        conn = self._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                subject     TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'pending',
                owner       TEXT,
                worktree    TEXT,
                created_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id       TEXT NOT NULL,
                depends_on_id TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on_id),
                FOREIGN KEY (task_id)       REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY (depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_owner
                ON tasks(status, owner);
            CREATE INDEX IF NOT EXISTS idx_deps_on
                ON task_dependencies(depends_on_id);
            """
        )

    # ------------------------------------------------------------------
    # 写：创建 / 依赖 / 绑定 worktree
    # ------------------------------------------------------------------

    def create_task(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
    ) -> str:
        """创建任务，返回全局唯一 task_id。"""
        task_id = f"task_{uuid.uuid4().hex}"
        created_at = time.time()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "INSERT INTO tasks(id, subject, description, status, owner, worktree, created_at) "
                    "VALUES(?, ?, ?, ?, NULL, NULL, ?)",
                    (task_id, subject, description, STATUS_PENDING, created_at),
                )
                for dep in blocked_by or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?, ?)",
                        (task_id, dep),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return task_id

    def add_dependency(self, task_id: str, depends_on_id: str) -> None:
        """给已存在任务追加一条依赖。"""
        with self._lock:
            if not self._exists(task_id):
                raise TaskNotFound(task_id)
            self._conn.execute(
                "INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?, ?)",
                (task_id, depends_on_id),
            )

    def bind_worktree(self, task_id: str, worktree_name: str) -> None:
        """把任务关联到一个 worktree（M1 Task 1.2 WorktreeSession 会调用）。"""
        with self._lock:
            if not self._exists(task_id):
                raise TaskNotFound(task_id)
            self._conn.execute(
                "UPDATE tasks SET worktree=? WHERE id=?",
                (worktree_name, task_id),
            )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Task:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            return _row_to_task(row)

    def list_tasks(self, status: str | None = None) -> list[Task]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY created_at", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at"
                ).fetchall()
            return [_row_to_task(r) for r in rows]

    def can_start(self, task_id: str) -> bool:
        """
        是否所有前置依赖都已完成（s20 can_start:123 的语义）。
        一条 JOIN 完成，避免 s20 版逐个 load 文件。
        """
        with self._lock:
            if not self._exists(task_id):
                raise TaskNotFound(task_id)
            # 未完成依赖计数：==0 即可启动
            count = self._conn.execute(
                """
                SELECT COUNT(*) FROM task_dependencies td
                JOIN tasks t ON td.depends_on_id = t.id
                WHERE td.task_id = ? AND t.status != ?
                """,
                (task_id, STATUS_COMPLETED),
            ).fetchone()[0]
            return count == 0

    def dependencies(self, task_id: str) -> list[str]:
        """某任务的直接前置依赖 id 列表。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT depends_on_id FROM task_dependencies WHERE task_id=?",
                (task_id,),
            ).fetchall()
            return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def claim_task(self, task_id: str, owner: str = "agent") -> bool:
        """
        原子认领任务（s20 claim_task:135 的并发安全核心）。

        用 UPDATE...WHERE status='pending' AND owner IS NULL 的原子性
        消除 s20 版 load→改→save 之间的竞态。多线程同时 claim 同一任务时，
        恰好一个成功。

        Returns:
            True 认领成功。
        Raises:
            TaskNotFound / TaskNotClaimable（reason 见 REASON_*）。
        """
        with self._lock:
            task = self.get_task(task_id)  # 不存在会 raise TaskNotFound

            # 依赖未满足
            if not self.can_start(task_id):
                blocked_by = [d for d in self.dependencies(task_id)
                              if self.get_task(d).status != STATUS_COMPLETED]
                raise TaskNotClaimable(task_id, REASON_BLOCKED, blocked_by)

            # 原子认领：仅当仍为 pending 且无人认领时更新
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, owner=? "
                "WHERE id=? AND status=? AND owner IS NULL",
                (STATUS_IN_PROGRESS, owner, task_id, STATUS_PENDING),
            )
            if cur.rowcount == 1:
                return True

            # 认领失败，查因。注意 complete/fail 不会清 owner，所以 owner 非空
            # 不代表「被别人认领中」——必须先按 status 判断。
            fresh = self.get_task(task_id)
            if fresh.status == STATUS_PENDING and fresh.owner is not None:
                raise TaskNotClaimable(task_id, REASON_ALREADY_OWNED)
            raise TaskNotClaimable(task_id, REASON_WRONG_STATUS)

    def complete_task(self, task_id: str) -> list[str]:
        """
        完成任务并返回被它解锁的 pending 任务 id 列表（s20 complete_task:156 的级联解锁）。

        Raises:
            TaskNotFound / TaskNotCompletable（不在 in_progress）。
        """
        with self._lock:
            task = self.get_task(task_id)
            if task.status != STATUS_IN_PROGRESS:
                raise TaskNotCompletable(task_id, task.status)

            conn = self._conn
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE tasks SET status=? WHERE id=? AND status=?",
                    (STATUS_COMPLETED, task_id, STATUS_IN_PROGRESS),
                )
                # 反向 JOIN：依赖本任务、且自身所有依赖都已完成的 pending 任务
                unblocked = [
                    r[0] for r in conn.execute(
                        """
                        SELECT t.id FROM tasks t
                        WHERE t.status = ?
                          AND t.id IN (SELECT td.task_id FROM task_dependencies td
                                       WHERE td.depends_on_id = ?)
                          AND NOT EXISTS (
                              SELECT 1 FROM task_dependencies td2
                              JOIN tasks dep ON td2.depends_on_id = dep.id
                              WHERE td2.task_id = t.id AND dep.status != ?
                          )
                        """,
                        (STATUS_PENDING, task_id, STATUS_COMPLETED),
                    ).fetchall()
                ]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return unblocked

    def fail_task(self, task_id: str) -> None:
        """置任务为 failed（M1 Task 1.3 demo 模拟崩溃回滚用；s20 无此状态）。"""
        with self._lock:
            task = self.get_task(task_id)
            if task.status not in (STATUS_IN_PROGRESS, STATUS_PENDING):
                raise TaskNotCompletable(task_id, task.status)
            self._conn.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                (STATUS_FAILED, task_id),
            )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "TaskEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _exists(self, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row is not None


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        subject=row["subject"],
        description=row["description"],
        status=row["status"],
        owner=row["owner"],
        worktree=row["worktree"],
        created_at=row["created_at"],
    )
