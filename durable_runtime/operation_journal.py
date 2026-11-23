"""Commit tool intent before execution and result before returning it."""

from dataclasses import asdict
import json
import time
import uuid

from agent.trace_v2 import redact_trace_value
from agent.task import EventType
from tools.base import ToolErrorType, ToolResult

from .run_manager import digest, encode
from .store import StoreFormatError


class OutcomeUnknown(RuntimeError):
    """A side effect needs reconciliation before execution can continue."""


class ToolOperation:
    def __init__(self, run, call_id: str, step: int, action_event_id: str | None, *,
                 kind: str = "tool", workspace_state=None):
        self.run = run
        self.call_id = call_id
        self.step = step
        self.action_event_id = action_event_id
        self.kind = kind
        self.workspace_state = workspace_state
        self.operation_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{run.run_id}:{call_id}:{kind}").hex
        self.parent_operation_id = None
        retry = run.store._conn.execute(
            """SELECT decision_id FROM recovery_decisions WHERE operation_id=?
               AND action='authorize_retry' ORDER BY created_at LIMIT 1""", (self.operation_id,),
        ).fetchone()
        if retry is not None:
            self.parent_operation_id = self.operation_id
            self.operation_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{self.operation_id}:{retry[0]}").hex

    def prepare(self, name, params, *, read_only: bool, decision, expected=None,
                policy_digest=None, approval_ref=None, worker_resume=False) -> ToolResult | None:
        run = self.run
        args = encode({"redacted": True} if name == "memory_propose" else redact_trace_value(params))
        # Compare the exact arguments even when stored display values are redacted.
        args_digest = digest(encode(params))
        permission = encode(redact_trace_value(
            {"decision": decision.decision.value, "reason": decision.reason}
            if decision is not None else {"decision": "delivery_authorized" if self.kind == "delivery" else "no_policy"}
        ))
        with run.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=? AND run_id=?",
                (self.operation_id, run.run_id),
            ).fetchone()
            if row is not None:
                if (row["name"], row["args_digest"], row["policy_digest"]) != (name, args_digest, policy_digest):
                    raise StoreFormatError("operation ID reused with different arguments or policy")
                if row["state"] == "result_committed":
                    if digest(encode([row["result_json"], row["observation_json"]])) != row["result_digest"]:
                        raise StoreFormatError("stored tool result digest mismatch")
                    saved = json.loads(row["result_json"])
                    if saved["error_type"] is not None:
                        saved["error_type"] = ToolErrorType(saved["error_type"])
                    saved["diagnostics"] = tuple(saved["diagnostics"])
                    result = ToolResult(**saved)
                elif row["state"] == "intent_committed":
                    result = None
                elif row["state"] == "outcome_unknown" and (
                    (worker_resume and row["replay_policy"] == "worker_resume")
                    or (read_only and row["replay_policy"] == "read_only" and run.borrowed_owner)
                ):
                    db.execute("UPDATE operations SET state='intent_committed', updated_at=? WHERE operation_id=?",
                               (time.time(), self.operation_id))
                    result = None
                else:
                    raise OutcomeUnknown(f"operation {self.operation_id} requires reconciliation")
            else:
                result = None
                if self.parent_operation_id is not None:
                    parent = db.execute("SELECT * FROM operations WHERE operation_id=?",
                                        (self.parent_operation_id,)).fetchone()
                    if (parent is None or parent["state"] != "resolved_not_applied" or
                            (parent["name"], parent["args_digest"], parent["policy_digest"],
                             parent["expected_postcondition_json"]) != (
                                 name, args_digest, policy_digest, encode(redact_trace_value(expected)),
                             )):
                        raise StoreFormatError("the authorized retry no longer matches the original operation")
                now = time.time()
                db.execute(
                    """INSERT INTO operations (operation_id, run_id, scope_id, step_id,
                       action_event_id, tool_call_id, parent_operation_id, operation_kind, name, args_json, args_digest, expected_postcondition_json,
                       effect, replay_policy, permission_json, policy_digest, approval_ref, state, owner_epoch, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent_committed', ?, ?, ?)""",
                    (self.operation_id, run.run_id, run.owner.scope_id, str(self.step), self.action_event_id,
                     self.call_id, self.parent_operation_id, self.kind, name, args, args_digest,
                     encode(redact_trace_value(expected)) if expected else None,
                     "read_only" if read_only else "may_mutate_repository",
                     "worker_resume" if worker_resume else "read_only" if read_only else "manual_only",
                     permission, policy_digest, approval_ref,
                     run.owner.owner_epoch, now, now),
                )
            if result is None and not read_only and db.execute(
                """SELECT 1 FROM operations WHERE scope_id=? AND state='outcome_unknown'
                   AND effect!='read_only' LIMIT 1""", (run.owner.scope_id,),
            ).fetchone():
                raise OutcomeUnknown("unresolved side effect blocks new mutations")
        if result is None:
            run.trace(EventType.TOOL_INTENT_COMMITTED, self.step, operation_id=self.operation_id, tool_name=name)
        return result

    def start(self) -> None:
        run = self.run
        with run.transaction() as db:
            changed = db.execute(
                """UPDATE operations SET state='started', owner_epoch=?, updated_at=?
                   WHERE operation_id=? AND run_id=? AND state='intent_committed'""",
                (run.owner.owner_epoch, time.time(), self.operation_id, run.run_id),
            )
            if changed.rowcount != 1:
                raise StoreFormatError("operation cannot enter started state")
        run.trace(EventType.TOOL_OPERATION_STARTED, self.step, operation_id=self.operation_id)

    def finish(self, name: str, result: ToolResult) -> None:
        run = self.run
        payload = encode(redact_trace_value(asdict(result)))
        observation = result.to_observation(name).to_dict()
        if self.workspace_state is not None:
            observation["committed_workspace"] = self.workspace_state()
        observation = encode(redact_trace_value(observation))
        ambiguous = False
        with run.transaction() as db:
            row = db.execute("SELECT effect FROM operations WHERE operation_id=?", (self.operation_id,)).fetchone()
            if self.kind == "tool" and name == "shell" and result.process_state is not None:
                ambiguous = result.process_state != "not_started" and (
                    result.process_state != "exited" or result.exit_code is None
                )
            else:
                ambiguous = (
                    row is not None and row["effect"] != "read_only"
                    and not result.success
                    and result.error_type not in {ToolErrorType.INVALID_ARGUMENTS, ToolErrorType.PERMISSION_DENIED}
                )
            changed = db.execute(
                """UPDATE operations SET state=?, result_json=?, result_digest=?,
                   observation_json=?, updated_at=? WHERE operation_id=? AND run_id=? AND state='started'
                   AND owner_epoch=?""",
                ("outcome_unknown" if ambiguous else "result_committed", payload, digest(encode([payload, observation])), observation,
                 time.time(), self.operation_id, run.run_id,
                 run.owner.owner_epoch),
            )
            if changed.rowcount != 1:
                raise StoreFormatError("operation cannot commit its result")
        process = ({"process_state": result.process_state, "exit_code": result.exit_code}
                   if result.process_state is not None else {})
        run.trace(EventType.TOOL_OUTCOME_UNKNOWN if ambiguous else EventType.TOOL_RESULT_COMMITTED,
                  self.step, operation_id=self.operation_id, **process)
        if ambiguous:
            raise OutcomeUnknown(f"operation {self.operation_id} returned without a confirmed effect")
