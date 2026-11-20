"""Collect durable evidence while each isolated pytest fixture is still available."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--forge-trial-evidence", default=None)


def _rows(db, query):
    return [dict(row) for row in db.execute(query)]


def collect_store(path: Path) -> dict:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        identity = json.loads(db.execute("SELECT value_json FROM runtime_meta WHERE key='project_identity'").fetchone()[0])
        runs = _rows(db, """SELECT run_id, task_id, scope_id, parent_run_id, status, entrypoint,
                                   config_digest, tool_registry_digest, revision, owner_epoch, checkpoint_id
                                   FROM runs ORDER BY run_id""")
        for run in runs:
            checkpoints = db.execute(
                """SELECT kind, revision, owner_epoch, payload_json, payload_digest
                   FROM run_checkpoints WHERE run_id=? ORDER BY revision""", (run["run_id"],),
            )
            run["checkpoints"] = []
            for row in checkpoints:
                payload = json.loads(row["payload_json"])
                result = payload.get("result") or payload.get("agent_result") or {}
                run["checkpoints"].append({
                    "kind": row["kind"], "revision": row["revision"], "owner_epoch": row["owner_epoch"],
                    "digest": row["payload_digest"], "repository_state": payload.get("repository_state"),
                    "agent_status": result.get("status"), "acceptance_status": result.get("acceptance_status"),
                    "delivery_status": result.get("delivery_status"), "usage": result.get("usage"),
                    "trace_reference": result.get("trace_path"),
                })
        return {
            "project_id": identity["project_id"], "database": str(path), "runs": runs,
            "owners": _rows(db, "SELECT scope_id, owner_epoch, revision FROM runtime_scopes ORDER BY scope_id"),
            "operations": _rows(db, """SELECT operation_id, run_id, parent_operation_id, tool_call_id, name, state,
                                      effect, replay_policy, owner_epoch, result_digest FROM operations ORDER BY created_at"""),
            "decisions": _rows(db, """SELECT operation_id, action, owner_epoch, request_digest, evidence_json
                                     FROM recovery_decisions ORDER BY created_at"""),
            "archive_records": _rows(db, """SELECT archive_id, scope_id, kind, payload_digest, source_ids_json,
                                            origin_run_id, origin_session_id
                                            FROM archive_records ORDER BY scope_id, sequence"""),
            "archive_projections": _rows(db, """SELECT scope_id, projection_kind, revision, messages_digest
                                               FROM archive_projections ORDER BY scope_id, projection_kind"""),
            "cache_events": _rows(db, """SELECT request_id, run_id, protocol, request_kind, attempt_index,
                                         logical_request_id, prompt_tokens, cached_tokens,
                                         cache_read_reported, estimated, prefix_hash, changed_components_json
                                         FROM cache_audit_events ORDER BY created_at"""),
            "model_calls": _rows(db, """SELECT model_call_id, run_id, step_id, status, request_digest
                                        FROM model_calls ORDER BY created_at"""),
        }


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    destination = item.config.getoption("--forge-trial-evidence")
    fixture = getattr(item, "funcargs", {}).get("tmp_path")
    if report.when != "call" or not destination:
        return report
    fixture = Path(fixture) if fixture is not None else None
    stores = []
    for db in sorted(fixture.rglob("runtime.db")) if fixture else ():
        try:
            stores.append(collect_store(db))
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            stores.append({"database": str(db), "collection_error": type(exc).__name__})
    traces = []
    transitions = []
    for trace in sorted(fixture.rglob("*.jsonl")) if fixture else ():
        content = trace.read_bytes()
        traces.append({"path": str(trace), "sha256": hashlib.sha256(content).hexdigest()})
        for raw in content.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if isinstance(payload, dict) and payload.get("operation_id") and "event_type" in row:
                transitions.append({
                    "event": row["event_type"], "trace": str(trace), "event_id": row.get("event_id"),
                    "operation_id": payload["operation_id"], "revision": payload.get("revision"),
                    "owner_epoch": payload.get("owner_epoch"),
                })
    with Path(destination).open("a", encoding="utf-8") as stream:
        measurements = dict(report.user_properties)
        stream.write(json.dumps({"node_id": report.nodeid, "stores": stores, "traces": traces,
                                 "measurements": measurements,
                                 "operation_transitions": transitions}, ensure_ascii=False) + "\n")
    return report
