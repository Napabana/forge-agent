"""
entry/api_store.py

SQLite-backed task metadata for the FastAPI service layer.

This is intentionally separate from task.engine.TaskEngine. TaskEngine tracks
the internal agent task graph; ApiTaskStore tracks HTTP request lifecycle and
points to the agent EventLog file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELED = "canceled"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCEL_REQUESTED)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED)


@dataclass
class ApiTask:
    id: str
    repo_path: str
    prompt: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result_summary: str | None = None
    error: str | None = None
    log_path: str | None = None
    forge_task_id: str | None = None
    options: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApiTaskNotFound(Exception):
    """Raised when an API task id does not exist."""


class ApiTaskStore:
    """
    Small SQLite store for API task metadata.

    Args:
        db_path: SQLite database path. Parent directories are created
                 automatically. Use ":memory:" for tests.
    """

    def __init__(self, db_path: str | Path = ".forge/api_tasks.db") -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_tasks (
                id             TEXT PRIMARY KEY,
                repo_path      TEXT NOT NULL,
                prompt         TEXT NOT NULL,
                status         TEXT NOT NULL,
                created_at     REAL NOT NULL,
                started_at     REAL,
                finished_at    REAL,
                result_summary TEXT,
                error          TEXT,
                log_path       TEXT,
                forge_task_id  TEXT,
                options_json   TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_api_tasks_status_created
                ON api_tasks(status, created_at);
            """
        )

    def create_task(
        self,
        *,
        repo_path: str,
        prompt: str,
        options: dict[str, Any] | None = None,
    ) -> str:
        task_id = f"api_{uuid.uuid4().hex}"
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO api_tasks(
                    id, repo_path, prompt, status, created_at, options_json
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    repo_path,
                    prompt,
                    STATUS_QUEUED,
                    now,
                    json.dumps(options or {}, ensure_ascii=False),
                ),
            )
        return task_id

    def mark_running(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE api_tasks
                SET status=?, started_at=COALESCE(started_at, ?)
                WHERE id=? AND status=?
                """,
                (STATUS_RUNNING, time.time(), task_id, STATUS_QUEUED),
            )
            if cur.rowcount == 0:
                if not self._exists(task_id):
                    raise ApiTaskNotFound(task_id)
                return False
            return True

    def set_runtime_info(
        self,
        task_id: str,
        *,
        log_path: str | None = None,
        forge_task_id: str | None = None,
    ) -> None:
        """Record runtime metadata as soon as the agent creates it."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE api_tasks
                SET log_path=COALESCE(?, log_path),
                    forge_task_id=COALESCE(?, forge_task_id)
                WHERE id=?
                """,
                (log_path, forge_task_id, task_id),
            )
            if cur.rowcount == 0:
                raise ApiTaskNotFound(task_id)

    def mark_finished(
        self,
        task_id: str,
        *,
        status: str,
        result_summary: str | None = None,
        error: str | None = None,
        log_path: str | None = None,
        forge_task_id: str | None = None,
    ) -> None:
        if status not in (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED):
            raise ValueError(f"invalid finished status: {status}")
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE api_tasks
                SET status=?, finished_at=?, result_summary=?, error=?,
                    log_path=COALESCE(?, log_path),
                    forge_task_id=COALESCE(?, forge_task_id)
                WHERE id=?
                """,
                (
                    status,
                    time.time(),
                    result_summary,
                    error,
                    log_path,
                    forge_task_id,
                    task_id,
                ),
            )
            if cur.rowcount == 0:
                raise ApiTaskNotFound(task_id)

    def request_cancel(self, task_id: str) -> str:
        """
        Request cancellation.

        Queued tasks become canceled immediately. Running tasks move to
        cancel_requested and rely on the agent loop to stop cooperatively.
        Terminal tasks are left unchanged.
        """
        with self._lock:
            task = self.get_task(task_id)
            if task.status == STATUS_QUEUED:
                self._conn.execute(
                    """
                    UPDATE api_tasks
                    SET status=?, finished_at=?, result_summary=?, error=NULL
                    WHERE id=?
                    """,
                    (STATUS_CANCELED, time.time(), "canceled before start", task_id),
                )
                return STATUS_CANCELED
            if task.status == STATUS_RUNNING:
                self._conn.execute(
                    "UPDATE api_tasks SET status=? WHERE id=?",
                    (STATUS_CANCEL_REQUESTED, task_id),
                )
                return STATUS_CANCEL_REQUESTED
            return task.status

    def mark_canceled(
        self,
        task_id: str,
        *,
        result_summary: str = "canceled",
        log_path: str | None = None,
        forge_task_id: str | None = None,
    ) -> None:
        self.mark_finished(
            task_id,
            status=STATUS_CANCELED,
            result_summary=result_summary,
            error=None,
            log_path=log_path,
            forge_task_id=forge_task_id,
        )

    def get_task(self, task_id: str) -> ApiTask:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise ApiTaskNotFound(task_id)
        return _row_to_task(row)

    def list_tasks(self, limit: int = 50) -> list[ApiTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def count_active(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            return int(self._conn.execute(
                f"SELECT COUNT(*) FROM api_tasks WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            ).fetchone()[0])

    def _exists(self, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM api_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_task(row: sqlite3.Row) -> ApiTask:
    options_raw = row["options_json"] or "{}"
    try:
        options = json.loads(options_raw)
    except json.JSONDecodeError:
        options = {}
    return ApiTask(
        id=row["id"],
        repo_path=row["repo_path"],
        prompt=row["prompt"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result_summary=row["result_summary"],
        error=row["error"],
        log_path=row["log_path"],
        forge_task_id=row["forge_task_id"],
        options=options,
    )
