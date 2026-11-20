"""SQLite state boundary for long-running agent executions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
from typing import Iterator

from project_memory.identity import ProjectIdentity


SCHEMA_VERSION = 3
PRODUCT = "forge-agent-durable-runtime"
_TABLES = frozenset({
    "runtime_meta", "runtime_scopes", "runs", "run_checkpoints", "model_calls", "operations",
    "recovery_decisions", "archive_records", "archive_projections", "cache_audit_events",
    "outbox_events", "workspace_trust",
})


class StoreFormatError(RuntimeError):
    """The database cannot safely be opened as this project's runtime store."""


class OwnerConflict(RuntimeError):
    """Another process owns the scope, or its origin cannot be verified."""


class RevisionConflict(RuntimeError):
    """The caller's scope revision or owner epoch is stale."""


@dataclass(frozen=True)
class OwnerReceipt:
    scope_id: str
    instance_id: str
    owner_epoch: int
    revision: int


class DurableRuntimeStore:
    """One project database; write transactions are fenced by scope ownership."""

    def __init__(self, identity: ProjectIdentity, root: str | Path | None = None, *, timeout_ms: int = 5000):
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise ValueError("durable runtime timeout must be a positive integer")
        if not identity.project_id or identity.project_id in {".", ".."} or Path(identity.project_id).name != identity.project_id or "\\" in identity.project_id:
            raise ValueError("invalid project ID")
        base = Path(root).expanduser() if root is not None else Path.home() / ".forge-agent" / "runtime"
        if not base.is_absolute():
            raise ValueError("durable runtime root must be absolute")
        self.identity = identity
        self.base = base / identity.project_id
        self._private_directory(self.base)
        self.locks = self.base / "locks"
        self.blobs = self.base / "blobs"
        for directory in (self.locks, self.blobs):
            self._private_directory(directory)
        self.path = self.base / "runtime.db"
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.is_symlink():
                raise StoreFormatError("runtime database sidecar cannot be a symlink")
            if sidecar.exists():
                self._restrict_file(sidecar)
        self._create_private_file(self.path)
        self._instance_id = uuid.uuid4().hex
        self._host_id = socket.gethostname()
        self._boot_id = self._read_boot_id()
        self._locks: dict[str, object] = {}
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=timeout_ms / 1000)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
            self._open_schema()
            if self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise StoreFormatError("SQLite WAL is unavailable")
            self._restrict_files()
        except (sqlite3.DatabaseError, ValueError) as exc:
            self.close()
            raise StoreFormatError(f"cannot open runtime database: {type(exc).__name__}: {exc}") from exc
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _private_directory(path: Path) -> None:
        if path.is_symlink():
            raise StoreFormatError(f"runtime directory cannot be a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            from .windows import restrict_access

            restrict_access(path, directory=True)
            return
        path.chmod(0o700)
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise StoreFormatError(f"runtime directory must have mode 0700: {path}")

    @staticmethod
    def _create_private_file(path: Path, *, exclusive: bool = False) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            if exclusive or path.is_symlink() or not path.is_file():
                raise
        else:
            os.close(fd)
        DurableRuntimeStore._restrict_file(path)

    @staticmethod
    def _restrict_file(path: Path) -> None:
        if os.name == "nt":
            from .windows import restrict_access

            restrict_access(path)
            return
        path.chmod(0o600)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise StoreFormatError(f"runtime file must have mode 0600: {path}")

    @staticmethod
    @lru_cache(maxsize=1)
    def _read_boot_id() -> str:
        if os.name == "nt":
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            try:
                result = subprocess.run(
                    [str(executable), "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks"],
                    capture_output=True, text=True, timeout=15, check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                value = result.stdout.strip()
                if value.isascii() and value.isdecimal():
                    return "windows:" + value
            except (OSError, subprocess.SubprocessError):
                pass
            raise OwnerConflict("Windows boot identity could not be verified")
        path = Path("/proc/sys/kernel/random/boot_id")
        if path.exists():
            return path.read_text(encoding="ascii").strip()
        raise OwnerConflict("system boot identity could not be verified")

    @staticmethod
    def _process_start(pid: int) -> int | None:
        if os.name == "nt":
            from .windows import process_creation_time

            return process_creation_time(pid)
        try:
            # Fields after the closing parenthesis start at field 3. Start time is field 22.
            return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        except (OSError, IndexError, ValueError):
            return None

    def _restrict_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                self._restrict_file(path)

    def _open_schema(self) -> None:
        with self._write():
            self._initialize_or_validate()

    def _initialize_or_validate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise StoreFormatError(f"unsupported runtime schema version: {version}")
        if version == 0 and self.path.stat().st_size > 0:
            tables = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                raise StoreFormatError("unrecognized database; original file preserved")
        if version == 0:
            schema = """
                CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE runtime_scopes (
                    scope_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
                    host_id TEXT NOT NULL, boot_id TEXT NOT NULL, pid INTEGER NOT NULL,
                    process_start REAL NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES runtime_scopes(scope_id),
                    task_id TEXT, parent_run_id TEXT, session_id TEXT, entrypoint TEXT NOT NULL,
                    status TEXT NOT NULL, task_snapshot_json TEXT NOT NULL, request_snapshot_json TEXT NOT NULL,
                    acceptance_contract_json TEXT, config_digest TEXT, tool_registry_digest TEXT,
                    checkpoint_id TEXT, step INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, owner_epoch INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE run_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    revision INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL, owner_epoch INTEGER NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE model_calls (
                    model_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), step_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL, request_fingerprint TEXT, status TEXT NOT NULL,
                    action_json TEXT, usage_json TEXT, provider_request_id TEXT, created_at REAL NOT NULL, finished_at REAL
                );
                CREATE TABLE operations (
                    operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    scope_id TEXT NOT NULL REFERENCES runtime_scopes(scope_id), parent_operation_id TEXT,
                    step_id TEXT, action_event_id TEXT, tool_call_id TEXT, operation_kind TEXT NOT NULL,
                    name TEXT NOT NULL, args_json TEXT NOT NULL, args_digest TEXT NOT NULL,
                    expected_postcondition_json TEXT, effect TEXT NOT NULL, replay_policy TEXT NOT NULL,
                    permission_json TEXT, approval_ref TEXT, policy_digest TEXT, state TEXT NOT NULL,
                    result_json TEXT, observation_json TEXT, result_digest TEXT, owner_epoch INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE recovery_decisions (
                    decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id), action TEXT NOT NULL,
                    actor TEXT NOT NULL, explanation TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    request_digest TEXT NOT NULL, created_at REAL NOT NULL, owner_epoch INTEGER NOT NULL
                );
                CREATE TABLE archive_records (
                    archive_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES runtime_scopes(scope_id),
                    sequence INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT, blob_ref TEXT,
                    payload_digest TEXT NOT NULL, source_ids_json TEXT NOT NULL, previous_id TEXT,
                    redaction_version INTEGER NOT NULL, created_at REAL NOT NULL,
                    origin_run_id TEXT REFERENCES runs(run_id), origin_session_id TEXT,
                    UNIQUE(scope_id, sequence)
                );
                CREATE TABLE archive_projections (
                    scope_id TEXT NOT NULL REFERENCES runtime_scopes(scope_id), projection_kind TEXT NOT NULL,
                    revision INTEGER NOT NULL, lineage_json TEXT NOT NULL, messages_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(scope_id, projection_kind)
                );
                CREATE TABLE cache_audit_events (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), session_id TEXT,
                    step_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL, protocol TEXT NOT NULL,
                    prompt_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER,
                    cache_read_reported INTEGER NOT NULL, estimated INTEGER NOT NULL,
                    component_hashes_json TEXT NOT NULL, changed_components_json TEXT NOT NULL,
                    prefix_hash TEXT NOT NULL, idle_ms INTEGER, created_at REAL NOT NULL,
                    request_kind TEXT NOT NULL DEFAULT 'generation', attempt_index INTEGER NOT NULL DEFAULT 1,
                    logical_request_id TEXT
                );
                CREATE TABLE outbox_events (
                    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    seq INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL, delivered_at REAL, UNIQUE(run_id, seq)
                );
                CREATE TABLE workspace_trust (
                    project_id TEXT PRIMARY KEY, repo_identity TEXT NOT NULL,
                    granted_scopes_json TEXT NOT NULL, trust_version INTEGER NOT NULL,
                    trusted_at REAL NOT NULL, revoked_at REAL
                );
                CREATE INDEX operations_by_scope_state ON operations(scope_id, state);
                CREATE INDEX checkpoints_by_run ON run_checkpoints(run_id, revision);
                CREATE INDEX model_calls_by_run ON model_calls(run_id, step_id);
                CREATE INDEX archive_by_scope ON archive_records(scope_id, sequence);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
            self._conn.executemany("INSERT INTO runtime_meta VALUES (?, ?)", [
                ("product", json.dumps(PRODUCT)),
                ("project_identity", json.dumps(self.identity.__dict__, sort_keys=True)),
                ("schema_version", json.dumps(SCHEMA_VERSION)),
            ])
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        meta = dict(self._conn.execute("SELECT key, value_json FROM runtime_meta"))
        if json.loads(meta.get("product", "null")) != PRODUCT or json.loads(meta.get("project_identity", "null")) != self.identity.__dict__:
            raise StoreFormatError("runtime database product or project identity mismatch")
        tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _TABLES.issubset(tables):
            raise StoreFormatError("runtime database is missing required tables")
        if version in (1, 2):
            # The writer transaction keeps this backup and the migration on the same source state.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source:
                self.backup(self.base / f"runtime-v{version}-before-v3-{uuid.uuid4().hex}.db", _source=source)
            if version == 1:
                self._conn.execute("ALTER TABLE archive_records ADD COLUMN origin_run_id TEXT REFERENCES runs(run_id)")
                self._conn.execute("ALTER TABLE archive_records ADD COLUMN origin_session_id TEXT")
            self._conn.execute("ALTER TABLE cache_audit_events ADD COLUMN request_kind TEXT NOT NULL DEFAULT 'generation'")
            self._conn.execute("ALTER TABLE cache_audit_events ADD COLUMN attempt_index INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("ALTER TABLE cache_audit_events ADD COLUMN logical_request_id TEXT")
            self._conn.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('schema_version', ?)",
                               (json.dumps(SCHEMA_VERSION),))
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _lock_file(file: object) -> None:
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            if file.read(1) != b"\0":
                file.seek(0)
                file.write(b"\0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(file: object) -> None:
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def claim(self, scope_id: str, *, previous_identity: tuple[str, str] | None = None) -> OwnerReceipt:
        if not scope_id or "/" in scope_id or "\\" in scope_id:
            raise ValueError("invalid scope ID")
        if scope_id in self._locks:
            raise OwnerConflict(f"scope already claimed by this store: {scope_id}")
        lock_path = self.locks / (hashlib.sha256(scope_id.encode()).hexdigest() + ".lock")
        self._create_private_file(lock_path)
        lock = lock_path.open("a+b")
        try:
            lock_path.chmod(0o600)
            try:
                self._lock_file(lock)
            except OSError as exc:
                raise OwnerConflict(f"scope is owned by another process: {scope_id}") from exc
            with self._write():
                row = self._conn.execute("SELECT * FROM runtime_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                old_identity = (row["host_id"], row["boot_id"]) if row else None
                different_origin = old_identity is not None and old_identity != (self._host_id, self._boot_id)
                if previous_identity is not None and (not different_origin or previous_identity != old_identity):
                    raise OwnerConflict("previous scope identity changed")
                if different_origin and previous_identity is None:
                    raise OwnerConflict("scope originated on another host or boot; explicit recovery required")
                if row and row["pid"] > 0 and not different_origin:
                    started = self._process_start(row["pid"])
                    if started is not None and started == row["process_start"]:
                        raise OwnerConflict("previous scope owner is still alive")
                epoch = row["owner_epoch"] + 1 if row else 1
                revision = row["revision"] + 1 if row else 1
                self._conn.execute("""
                    INSERT INTO runtime_scopes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_id) DO UPDATE SET instance_id=excluded.instance_id,
                    owner_epoch=excluded.owner_epoch, pid=excluded.pid,
                    host_id=excluded.host_id, boot_id=excluded.boot_id,
                    process_start=excluded.process_start, revision=excluded.revision
                """, (scope_id, self._instance_id, epoch, self._host_id, self._boot_id, os.getpid(),
                      self._process_start(os.getpid()) or 0, revision))
                if different_origin:
                    self._conn.execute(
                        "INSERT INTO runtime_meta VALUES (?, ?)",
                        (f"takeover:{uuid.uuid4().hex}", json.dumps({
                            "scope_id": scope_id, "previous_host": old_identity[0], "previous_boot": old_identity[1],
                            "host": self._host_id, "boot": self._boot_id, "owner_epoch": epoch, "at": time.time(),
                        })),
                    )
                self._conn.execute("UPDATE operations SET state='outcome_unknown', updated_at=? WHERE scope_id=? AND state='started'", (time.time(), scope_id))
                self._conn.execute(
                    """UPDATE model_calls SET status='outcome_unknown', finished_at=?
                       WHERE status='started' AND run_id IN (SELECT run_id FROM runs WHERE scope_id=?)""",
                    (time.time(), scope_id),
                )
                self._conn.execute(
                    """UPDATE runs SET status=CASE WHEN EXISTS (
                             SELECT 1 FROM operations o WHERE o.run_id=runs.run_id AND o.state='outcome_unknown'
                             AND o.replay_policy!='worker_resume'
                             AND NOT (runs.entrypoint='worker' AND runs.scope_id!=runs.run_id AND o.replay_policy='read_only')
                       ) THEN 'reconciliation_required' ELSE 'interrupted' END,
                       owner_epoch=?, revision=?, updated_at=?
                       WHERE scope_id=? AND status IN (
                           'running', 'delivery_pending', 'acceptance_pending',
                           'acceptance_passed', 'acceptance_failed', 'acceptance_skipped'
                       )""",
                    (epoch, revision, time.time(), scope_id),
                )
            self._locks[scope_id] = lock
            return OwnerReceipt(scope_id, self._instance_id, epoch, revision)
        except BaseException:
            lock.close()
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self, owner: OwnerReceipt, expected_revision: int | None) -> Iterator[sqlite3.Connection]:
        """Atomically fence a write and advance its scope revision on commit."""
        if owner.scope_id not in self._locks and expected_revision is not None:
            raise OwnerConflict("scope lock is not held")
        with self._write():
            row = self._conn.execute(
                "SELECT instance_id, owner_epoch, revision, pid, host_id, boot_id FROM runtime_scopes WHERE scope_id=?",
                (owner.scope_id,),
            ).fetchone()
            if (row is None or (row[0], row[1], row[3], row[4], row[5]) !=
                    (owner.instance_id, owner.owner_epoch, os.getpid(), self._host_id, self._boot_id)
                    or expected_revision is not None and row[2] != expected_revision):
                raise RevisionConflict("stale owner epoch or scope revision")
            yield self._conn
            self._conn.execute("UPDATE runtime_scopes SET revision=revision+1 WHERE scope_id=?", (owner.scope_id,))

    def revision(self, scope_id: str) -> int:
        row = self._conn.execute("SELECT revision FROM runtime_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if row is None:
            raise KeyError(scope_id)
        return row[0]

    def pause_marker(self, run_id: str) -> Path:
        return self.locks / (hashlib.sha256(run_id.encode()).hexdigest() + ".pause")

    def request_pause(self, run_id: str) -> None:
        row = self._conn.execute("SELECT status, entrypoint, request_snapshot_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise StoreFormatError("only a running Run can be paused")
        if row["entrypoint"] != "cli" or not json.loads(row["request_snapshot_json"]).get("resumable"):
            raise StoreFormatError("this Run does not support pause and resume")
        self._create_private_file(self.pause_marker(run_id))

    def integrity_check(self) -> None:
        result = self._conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise StoreFormatError(f"runtime database integrity check failed: {result}")

    def prune_cache_audit(self, retention_days: int) -> int:
        if type(retention_days) is not int or retention_days < 1:
            raise ValueError("cache retention must be a positive number of days")
        with self._write() as db:
            return db.execute("DELETE FROM cache_audit_events WHERE created_at<?",
                              (time.time() - retention_days * 86400,)).rowcount

    def backup(self, destination: str | Path, *, _source: sqlite3.Connection | None = None) -> Path:
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._create_private_file(target, exclusive=True)
        copied = []
        blob_backup = target.with_name(target.name + ".blobs")
        created_blob_dir = False
        try:
            with closing(sqlite3.connect(target)) as backup:
                (self._conn if _source is None else _source).backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StoreFormatError("runtime backup did not pass integrity check")
                refs = backup.execute("SELECT DISTINCT blob_ref FROM archive_records WHERE blob_ref IS NOT NULL").fetchall()
            if refs:
                blob_backup.mkdir(mode=0o700)
                created_blob_dir = True
                self._private_directory(blob_backup)
            for (checksum,) in refs:
                if not re.fullmatch(r"[a-f0-9]{64}", checksum):
                    raise StoreFormatError("invalid archive Blob identifier in backup")
                source = self.blobs / checksum
                if source.is_symlink() or not source.is_file():
                    raise StoreFormatError("cannot back up a missing archive Blob")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != checksum:
                    raise StoreFormatError("cannot back up a damaged archive Blob")
                destination = blob_backup / checksum
                self._create_private_file(destination, exclusive=True)
                copied.append(destination)
                with destination.open("wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException:
            for attachment in copied:
                attachment.unlink(missing_ok=True)
            if created_blob_dir:
                blob_backup.rmdir()
            target.unlink()
            raise
        return target

    def close(self) -> None:
        try:
            if self._locks:
                with self._write():
                    self._conn.execute("UPDATE runtime_scopes SET pid=0 WHERE instance_id=?", (self._instance_id,))
        finally:
            for lock in self._locks.values():
                self._unlock_file(lock)
                lock.close()
            self._locks.clear()
            self._conn.close()

    def __enter__(self) -> DurableRuntimeStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
