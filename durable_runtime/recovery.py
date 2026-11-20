"""Operator decisions for effects that cannot be safely inferred."""

import json
import time

from agent.trace_v2 import redact_trace_value

from .run_manager import digest, encode
from .store import StoreFormatError


RESOLUTIONS = {
    "resolve_applied": "resolved_applied",
    "resolve_not_applied": "resolved_not_applied",
    "accept_unknown": None,
}


def resolve_operation(store, operation_id: str, decision_id: str, *, action: str,
                      actor: str, reason: str, evidence: dict) -> None:
    if action not in RESOLUTIONS or not reason.strip() or not actor.strip() or not evidence:
        raise ValueError("a supported action, actor, reason and evidence are required")
    row = store._conn.execute(
        "SELECT scope_id, run_id FROM operations WHERE operation_id=?", (operation_id,),
    ).fetchone()
    if row is None:
        raise StoreFormatError("operation does not exist in this project")
    request = {"operation_id": operation_id, "action": action, "actor": actor,
               "reason": reason, "evidence": evidence}
    request_digest = digest(encode(request))
    owner = store.claim(row["scope_id"])
    with store.transaction(owner, owner.revision) as db:
        existing = db.execute(
            "SELECT request_digest FROM recovery_decisions WHERE decision_id=?", (decision_id,),
        ).fetchone()
        if existing:
            if existing[0] != request_digest:
                raise StoreFormatError("decision ID reused with different evidence")
            return
        current = db.execute("SELECT state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if current[0] != "outcome_unknown":
            raise StoreFormatError("only unknown outcomes can be resolved")
        db.execute(
            """INSERT INTO recovery_decisions (decision_id, run_id, operation_id, action, actor,
               explanation, evidence_json, request_digest, created_at, owner_epoch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, row["run_id"], operation_id, action, redact_trace_value(actor),
             redact_trace_value(reason), encode(redact_trace_value(evidence)), request_digest,
             time.time(), owner.owner_epoch),
        )
        if RESOLUTIONS[action]:
            db.execute("UPDATE operations SET state=?, updated_at=? WHERE operation_id=?",
                       (RESOLUTIONS[action], time.time(), operation_id))
            db.execute(
                """UPDATE runs SET status='interrupted', revision=?, updated_at=? WHERE run_id=?
                   AND status='reconciliation_required' AND NOT EXISTS (
                       SELECT 1 FROM operations WHERE run_id=runs.run_id AND state='outcome_unknown'
                   )""",
                (owner.revision + 1, time.time(), row["run_id"]),
            )


def authorize_file_retry(store, operation_id: str, decision_id: str, *, actor: str, reason: str) -> None:
    """Authorize one attempt with a verified file precondition or absent Git ref."""
    from .reconcilers import inspect_file_operation, inspect_remote_delivery

    row = store._conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None or row["state"] != "resolved_not_applied" or not reason.strip():
        raise StoreFormatError("a resolved-not-applied operation and reason are required")
    owner = store.claim(row["scope_id"])
    if row["name"] == "git_push" and row["operation_kind"] == "delivery":
        evidence = inspect_remote_delivery(row, store.identity)
        required_outcome = "not_applied"
    else:
        evidence = inspect_file_operation(row, store.identity)
        required_outcome = "precondition_matches"
    if evidence is None or evidence["outcome"] != required_outcome:
        raise StoreFormatError("the operation precondition could not be verified")
    with store.transaction(owner, owner.revision) as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        existing = db.execute(
            "SELECT 1 FROM recovery_decisions WHERE operation_id=? AND action='authorize_retry'",
            (operation_id,),
        ).fetchone()
        if row["state"] != "resolved_not_applied" or existing:
            raise StoreFormatError("a retry has already been authorized or the operation changed")
        serialized = encode(redact_trace_value(evidence))
        db.execute(
            """INSERT INTO recovery_decisions (decision_id, run_id, operation_id, action, actor,
               explanation, evidence_json, request_digest, created_at, owner_epoch)
               VALUES (?, ?, ?, 'authorize_retry', ?, ?, ?, ?, ?, ?)""",
            (decision_id, row["run_id"], operation_id, redact_trace_value(actor),
             redact_trace_value(reason), serialized, digest(encode([operation_id, actor, reason, serialized])),
             time.time(), owner.owner_epoch),
        )


def reconcile_delivery(run) -> str:
    """Finalize a delivery whose PR creation returned no local receipt."""
    import uuid
    from .reconcilers import inspect_remote_delivery

    row = run.store._conn.execute(
        """SELECT * FROM operations WHERE run_id=? AND scope_id=?
           AND operation_kind='delivery' AND name='github_create_pr' AND state='outcome_unknown'""",
        (run.run_id, run.owner.scope_id),
    ).fetchone()
    if row is None:
        raise StoreFormatError("there is no unknown PR creation to reconcile")
    evidence = inspect_remote_delivery(row, run.store.identity)
    expected = json.loads(row["expected_postcondition_json"] or "{}")
    url = evidence.get("url") if evidence and evidence.get("outcome") == "applied" else None
    expected_url = f"https://github.com/{expected.get('repo')}/pull/{evidence.get('pull_request_number')}" if evidence else None
    if (not isinstance(url, str) or url.casefold() != expected_url.casefold()
            or evidence.get("head_commit") != expected.get("target_commit")):
        raise StoreFormatError("could not verify a matching GitHub PR receipt")
    state = run.load_checkpoint()
    result = state.get("result") or {}
    if result.get("status") != "success" or result.get("acceptance_status") != "passed":
        raise StoreFormatError("saved Agent and acceptance results do not authorize delivery")
    serialized = encode(redact_trace_value(evidence))
    now = time.time()
    with run.transaction() as db:
        if db.execute(
            """SELECT 1 FROM operations WHERE scope_id=? AND state='outcome_unknown'
               AND operation_id!=? AND effect!='read_only' LIMIT 1""",
            (run.owner.scope_id, row["operation_id"]),
        ).fetchone():
            raise StoreFormatError("another side effect still needs reconciliation")
        db.execute(
            """INSERT INTO recovery_decisions (decision_id, run_id, operation_id, action, actor,
               explanation, evidence_json, request_digest, created_at, owner_epoch)
               VALUES (?, ?, ?, 'resolve_applied', 'github_reconciler', ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, run.run_id, row["operation_id"], "verified matching GitHub PR",
             serialized, digest(serialized), now, run.owner.owner_epoch),
        )
        db.execute(
            "UPDATE operations SET state='resolved_applied', updated_at=? WHERE operation_id=?",
            (now, row["operation_id"]),
        )
        run._checkpoint(db, state["next_step"], "delivered", {
            **state, "result": {**result, "delivery_status": "delivered"}, "pr_url": url,
        }, status="delivered")
    return url
