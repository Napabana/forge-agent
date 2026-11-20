"""Runtime durability and ownership fencing contracts."""

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from durable_runtime import DurableRuntimeStore, OwnerConflict, RevisionConflict, StoreFormatError
from project_memory.identity import ProjectIdentity


def identity(project_id="test-project"):
    return ProjectIdentity(project_id, "path", "/tmp/test-project")


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS ACL")
def test_windows_runtime_acl_is_private(tmp_path, monkeypatch):
    from durable_runtime.windows import current_user_sid

    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with DurableRuntimeStore(identity(), tmp_path) as store:
        store.claim("acl-scope")
        backup = store.backup(tmp_path / "backup.db")
        for path in (store.base, store.path, store.locks, *store.locks.glob("*.lock"), backup):
            monkeypatch.setenv("FORGE_TEST_ACL_PATH", str(path))
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                 "$ErrorActionPreference = 'Stop'; $path = $env:FORGE_TEST_ACL_PATH; "
                 "$acl = if ([IO.Directory]::Exists($path)) { [IO.Directory]::GetAccessControl($path) } "
                 "else { [IO.File]::GetAccessControl($path) }; "
                 "$acl.AreAccessRulesProtected; "
                 "$acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | "
                 "ForEach-Object { $_.IdentityReference.Value }"],
                capture_output=True, text=True, errors="replace", timeout=15, check=False,
            )
            assert result.returncode == 0, (path, result.stdout, result.stderr)
            assert result.stdout.splitlines() == ["True", current_user_sid()]


def test_claim_fences_writers_and_revisions(tmp_path, record_property):
    with DurableRuntimeStore(identity(), tmp_path) as store:
        owner = store.claim("session-1")
        with DurableRuntimeStore(identity(), tmp_path) as competitor:
            with pytest.raises(OwnerConflict):
                competitor.claim("session-1")
            other = competitor.claim("session-2")
            assert other.owner_epoch == 1
        with store.transaction(owner, owner.revision) as db:
            db.execute("INSERT INTO runtime_meta VALUES ('test', '1')")
        assert store.revision("session-1") == owner.revision + 1
        with pytest.raises(RevisionConflict):
            with store.transaction(owner, owner.revision) as db:
                db.execute("INSERT INTO runtime_meta VALUES ('rollback', '1')")
        assert store._conn.execute("SELECT 1 FROM runtime_meta WHERE key='rollback'").fetchone() is None
    with DurableRuntimeStore(identity(), tmp_path) as successor:
        assert successor.claim("session-1").owner_epoch == 2
        with pytest.raises(RevisionConflict):
            with successor.transaction(owner, owner.revision):
                pytest.fail("previous Owner wrote after take over")
        record_property("owner_fencing_correct", 1)


def test_shared_worker_owner_is_fenced_after_lead_releases_scope(tmp_path, record_property):
    from agent.task import Task
    from durable_runtime.run_manager import DurableRun

    with DurableRuntimeStore(identity(), tmp_path) as worker_store:
        with DurableRuntimeStore(identity(), tmp_path) as lead_store:
            lead = DurableRun(lead_store, "lead", "shared")
            lead.create(Task("lead", str(tmp_path)), "cli")
            worker = DurableRun(worker_store, "child", "shared", parent_owner=lead.owner)
            worker.create(Task("worker", str(tmp_path)), "worker", parent_run_id="lead")
            lead.checkpoint(2, "lead", {})
            worker.checkpoint(2, "worker", {})
            assert worker.revision == lead.revision + 1
        with pytest.raises(RevisionConflict):
            worker.checkpoint(3, "stale", {})
        with DurableRuntimeStore(identity(), tmp_path) as next_store:
            assert next_store.claim("shared").owner_epoch == lead.owner.owner_epoch + 1
            with pytest.raises(RevisionConflict):
                worker.checkpoint(3, "stale", {})
            with pytest.raises(RevisionConflict):
                with next_store.transaction(lead.owner, next_store.revision("shared")):
                    pytest.fail("previous Lead receipt still has write access")
    record_property("owner_fencing_correct", 1)


def test_tool_journal_recovery_at_each_boundary(tmp_path, record_property):
    from agent.task import Task
    from durable_runtime.operation_journal import OutcomeUnknown, ToolOperation
    from durable_runtime.run_manager import DurableRun
    from tools.base import ToolResult

    expected = ToolResult(True, "fixture receipt")
    for kind, read_only in [("tool", False), ("tool", True), ("delivery", False)]:
        for boundary in ("intent", "started", "result"):
            scope = f"{kind}-{read_only}-{boundary}"
            with DurableRuntimeStore(identity(), tmp_path) as store:
                run = DurableRun(store, scope, scope)
                run.create(Task("fixture", str(tmp_path)), "cli")
                run.checkpoint(1, "running", {}, status="running")
                operation = ToolOperation(run, "call", 1, None, kind=kind)
                assert operation.prepare("fixture", {}, read_only=read_only, decision=None) is None
                if boundary != "intent":
                    operation.start()
                if boundary == "result":
                    operation.finish("fixture", expected)
            with DurableRuntimeStore(identity(), tmp_path) as store:
                run = DurableRun(store, scope, scope)
                recovered = ToolOperation(run, "call", 1, None, kind=kind)
                assert recovered.operation_id == operation.operation_id
                if boundary == "started":
                    with pytest.raises(OutcomeUnknown):
                        recovered.prepare("fixture", {}, read_only=read_only, decision=None)
                    continue
                result = recovered.prepare("fixture", {}, read_only=read_only, decision=None)
                assert result == (expected if boundary == "result" else None)
    record_property("operation_boundaries_checked", 9)


def test_started_operation_becomes_unknown_on_takeover(tmp_path, record_property):
    with DurableRuntimeStore(identity(), tmp_path) as store:
        owner = store.claim("run-1")
        now = time.time()
        with store.transaction(owner, owner.revision) as db:
            db.execute("""INSERT INTO runs (run_id, scope_id, entrypoint, status,
                task_snapshot_json, request_snapshot_json, revision, owner_epoch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("run-1", "run-1", "cli", "running", "{}", "{}", 1, 1, now, now))
            db.execute("""INSERT INTO operations (operation_id, run_id, scope_id, operation_kind,
                name, args_json, args_digest, effect, replay_policy, state, owner_epoch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("op-1", "run-1", "run-1", "tool", "write", "{}", "digest", "MUTATING",
                 "MANUAL_ONLY", "started", 1, now, now))
    with DurableRuntimeStore(identity(), tmp_path) as resumed:
        resumed.claim("run-1")
        assert resumed._conn.execute("SELECT state FROM operations WHERE operation_id='op-1'").fetchone()[0] == "outcome_unknown"
        record_property("unknown_outcome_detected", 1)


def test_different_boot_requires_matching_explicit_takeover(tmp_path, monkeypatch):
    with DurableRuntimeStore(identity(), tmp_path) as store:
        original = store.claim("scope")
        previous = store._host_id, store._boot_id
    monkeypatch.setattr(DurableRuntimeStore, "_read_boot_id", staticmethod(lambda: "fixture-new-boot"))
    with DurableRuntimeStore(identity(), tmp_path) as store:
        with pytest.raises(OwnerConflict, match="explicit recovery"):
            store.claim("scope")
        with pytest.raises(OwnerConflict, match="identity changed"):
            store.claim("scope", previous_identity=(previous[0], "stale"))
        adopted = store.claim("scope", previous_identity=previous)
        assert adopted.owner_epoch == original.owner_epoch + 1
        assert store._conn.execute("SELECT count(*) FROM runtime_meta WHERE key LIKE 'takeover:%'").fetchone()[0] == 1


def test_identity_schema_backup_and_integrity(tmp_path):
    with DurableRuntimeStore(identity(), tmp_path) as store:
        store.integrity_check()
        backup = store.backup(tmp_path / "backup.db")
        assert sqlite3.connect(backup).execute("PRAGMA user_version").fetchone()[0] == 3
    with pytest.raises(StoreFormatError, match="identity mismatch"):
        # Same database path with a forged identity is rejected, without changing the file.
        forged = identity()
        object.__setattr__(forged, "key", "different")
        DurableRuntimeStore(forged, tmp_path)
    db = tmp_path / identity().project_id / "runtime.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(StoreFormatError, match="unsupported"):
        DurableRuntimeStore(identity(), tmp_path)
    assert db.exists()


def test_absolute_root_required(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        DurableRuntimeStore(identity(), "relative/runtime")
    with pytest.raises(ValueError, match="project ID"):
        DurableRuntimeStore(identity("../escape"), tmp_path)


@pytest.mark.parametrize("version", [1, 2])
def test_old_database_is_backed_up_before_migration(tmp_path, version):
    from contextlib import closing

    with DurableRuntimeStore(identity(), tmp_path) as old:
        old.claim("retained")
        path = old.path
    with closing(sqlite3.connect(path)) as v1:
        if version == 1:
            v1.execute("ALTER TABLE archive_records DROP COLUMN origin_run_id")
            v1.execute("ALTER TABLE archive_records DROP COLUMN origin_session_id")
        for column in ("request_kind", "attempt_index", "logical_request_id"):
            v1.execute(f"ALTER TABLE cache_audit_events DROP COLUMN {column}")
        v1.execute(f"PRAGMA user_version={version}")
        v1.execute("UPDATE runtime_meta SET value_json=? WHERE key='schema_version'", (str(version),))
        v1.commit()
    with DurableRuntimeStore(identity(), tmp_path) as migrated:
        assert migrated.revision("retained") == 1
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row[1] for row in migrated._conn.execute("PRAGMA table_info(archive_records)")}
        assert {"origin_run_id", "origin_session_id"} <= columns
        backup, = migrated.base.glob(f"runtime-v{version}-before-v3-*.db")
    with closing(sqlite3.connect(backup)) as v1:
        assert v1.execute("PRAGMA user_version").fetchone()[0] == version
        assert v1.execute("SELECT scope_id FROM runtime_scopes").fetchone()[0] == "retained"
