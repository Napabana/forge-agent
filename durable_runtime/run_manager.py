"""Fenced Run checkpoints and a journal for parsed model responses."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
import uuid
from dataclasses import asdict

from agent.task import Action, ActionType, EventType, ToolCall
from agent.trace_v2 import REDACTED, is_sensitive_trace_key, redact_trace_value
from llm.base import LLMResponse
from llm.usage import TokenUsage

from .store import DurableRuntimeStore, StoreFormatError

logger = logging.getLogger(__name__)


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def redact_history_payload(value):
    """Redact provider-native JSON arguments as structured data."""
    if isinstance(value, dict):
        safe = {key: REDACTED if is_sensitive_trace_key(key) else redact_history_payload(item)
                for key, item in value.items()}
        call = safe.get("responses_call")
        if isinstance(call, dict) and isinstance(call.get("arguments"), str):
            try:
                raw = json.loads(call["arguments"])
                redacted = redact_trace_value(raw)
                if raw != redacted:
                    call["arguments"] = encode(redacted)
            except ValueError:
                call["arguments"] = "[REDACTED]"
        return safe
    if isinstance(value, (list, tuple)):
        return type(value)(redact_history_payload(item) for item in value)
    return redact_trace_value(value)


def checked_payload(payload: str, expected_digest: str) -> dict:
    if digest(payload) != expected_digest:
        raise StoreFormatError("checkpoint digest mismatch")
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise StoreFormatError("unsupported checkpoint payload")
    return data


class DurableRun:
    """A Run bound to an acquired scope. One caller advances its revision."""

    def __init__(self, store: DurableRuntimeStore, run_id: str, scope_id: str, *, parent_owner=None):
        self.store = store
        self.run_id = run_id
        self.borrowed_owner = parent_owner is not None
        self.owner = parent_owner if parent_owner is not None else store.claim(scope_id)
        if self.owner.scope_id != scope_id:
            raise StoreFormatError("Run scope does not match its Owner")
        self.revision = self.owner.revision
        self.restoring = False
        self.archive = None
        self.log = None
        self.worktree = None
        self.path_acceptance = None

    @contextmanager
    def transaction(self):
        # A sibling Run may have written since the previous step. Refresh under the SQLite writer lock.
        with self.store.transaction(self.owner, None) as db:
            self.revision = self.store.revision(self.owner.scope_id)
            yield db
        self.revision += 1

    def trace(self, event: EventType, step: int, **details) -> None:
        if self.log is None:
            return
        try:
            self.log.log_trace(event, step, run_id=self.run_id, scope_id=self.owner.scope_id,
                               owner_epoch=self.owner.owner_epoch, revision=self.revision, **details)
        except Exception as exc:
            logger.warning("Could not append durable trace event (%s)", type(exc).__name__)

    def create(self, task, entrypoint: str, *, session_id=None, parent_run_id=None,
               request=None, acceptance=None, config_digest=None, tool_registry_digest=None) -> None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO runs (run_id, scope_id, task_id, entrypoint, status,
                   task_snapshot_json, request_snapshot_json, session_id, parent_run_id,
                   acceptance_contract_json, config_digest, tool_registry_digest,
                   revision, owner_epoch, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.run_id, self.owner.scope_id, task.task_id, entrypoint,
                 encode(redact_trace_value(task.to_dict())), encode(redact_trace_value(request or {})),
                 session_id, parent_run_id, encode(redact_trace_value(acceptance or {})),
                 config_digest, tool_registry_digest, self.revision + 1,
                 self.owner.owner_epoch, now, now),
            )
            self._checkpoint(db, 1, "created", {})

    def _checkpoint(self, db, step: int, kind: str, state: dict, *, status: str | None = None) -> str:
        checkpoint_id = uuid.uuid4().hex
        if self.archive is not None and "history" in state:
            canonical_revision = self.archive.capture_history(db, state["history"])
            state = {**state, "canonical_archive_revision": canonical_revision}
            model_revision, _ = self.archive.projection(db, "model_view")
            if "model_history" in state:
                compaction = (state.get("compaction_state") or {}).get("active_view") or {}
                model_revision = self.archive.rewrite_model_view(
                    db, state.pop("model_history"), model_revision, compaction.get("source_end_index"),
                )
            state["model_archive_revision"] = model_revision
        value = {
            **state, "schema_version": 1, "run_id": self.run_id, "scope_id": self.owner.scope_id,
            "next_step": step, "revision": self.revision + 1,
        }
        if self.worktree is not None:
            value["worktree"] = self.worktree
        if self.path_acceptance is not None:
            value["path_acceptance"] = self.path_acceptance
        safe = redact_history_payload(value)
        payload = encode({**safe, "redacted": safe != value or state.get("redacted", False)})
        now = time.time()
        db.execute(
            """INSERT INTO run_checkpoints
               (checkpoint_id, run_id, revision, kind, payload_json, payload_digest, owner_epoch, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (checkpoint_id, self.run_id, self.revision + 1, kind, payload, digest(payload),
             self.owner.owner_epoch, now),
        )
        changed = db.execute(
            """UPDATE runs SET checkpoint_id=?, step=?, updated_at=?, owner_epoch=?, revision=?,
               status=COALESCE(?, status) WHERE run_id=? AND scope_id=?""",
            (checkpoint_id, step - 1, now, self.owner.owner_epoch, self.revision + 1, status,
             self.run_id, self.owner.scope_id),
        )
        if changed.rowcount != 1:
            raise StoreFormatError("Run does not belong to the claimed scope")
        return checkpoint_id

    def checkpoint(self, step: int, kind: str, state: dict, *, status: str | None = None) -> str:
        with self.transaction() as db:
            checkpoint_id = self._checkpoint(db, step, kind, state, status=status)
        self.trace(EventType.RUN_CHECKPOINT_COMMITTED, step - 1, checkpoint_id=checkpoint_id, kind=kind)
        return checkpoint_id

    def load_checkpoint(self) -> dict:
        row = self.store._conn.execute(
            """SELECT c.payload_json, c.payload_digest FROM runs r
               JOIN run_checkpoints c ON c.checkpoint_id=r.checkpoint_id AND c.run_id=r.run_id
               WHERE r.run_id=? AND r.scope_id=?""", (self.run_id, self.owner.scope_id),
        ).fetchone()
        if row is None:
            raise StoreFormatError("Run has no checkpoint in this scope")
        data = checked_payload(*row)
        if (data["run_id"], data["scope_id"]) != (self.run_id, self.owner.scope_id):
            raise StoreFormatError("checkpoint identity mismatch")
        return data

    def resume(self, task, entrypoint: str, acceptance: dict, config_digest: str,
               tool_registry_digest: str, request_snapshot: dict | None = None) -> None:
        row = self.store._conn.execute(
            """SELECT status, task_snapshot_json, entrypoint, acceptance_contract_json, config_digest,
                      tool_registry_digest, request_snapshot_json FROM runs WHERE run_id=? AND scope_id=?""",
            (self.run_id, self.owner.scope_id),
        ).fetchone()
        if row is None or row["status"] not in {"interrupted", "paused"}:
            raise StoreFormatError("only interrupted runs without unresolved outcomes can resume")
        if self.store._conn.execute(
            """SELECT 1 FROM operations o JOIN runs r ON r.run_id=o.run_id
               WHERE o.scope_id=? AND o.state='outcome_unknown' AND o.replay_policy!='worker_resume'
               AND NOT (r.entrypoint='worker' AND r.scope_id!=r.run_id AND o.replay_policy='read_only') LIMIT 1""",
            (self.owner.scope_id,),
        ).fetchone():
            raise StoreFormatError("scope has unresolved tool outcomes")
        expected = (encode(task.to_dict()), entrypoint, encode(acceptance), config_digest,
                    tool_registry_digest, encode(request_snapshot or {"isolate": False, "sandbox": False, "resumable": True}))
        if tuple(row)[1:] != expected:
            raise StoreFormatError("saved task, acceptance, model configuration or tool registry differs")
        checkpoint = self.load_checkpoint()
        if checkpoint.get("redacted"):
            raise StoreFormatError("checkpoint contains redacted execution state")
        self.worktree = checkpoint.get("worktree")
        self.path_acceptance = checkpoint.get("path_acceptance")
        if request_snapshot and request_snapshot.get("isolate") and self.worktree is None:
            raise StoreFormatError("no saved worktree binding can be resumed")
        self.store.pause_marker(self.run_id).unlink(missing_ok=True)
        self.restoring = True

    def has_unknown_model_usage(self) -> bool:
        return self.store._conn.execute(
            "SELECT 1 FROM model_calls WHERE run_id=? AND status='outcome_unknown' LIMIT 1",
            (self.run_id,),
        ).fetchone() is not None

    def committed_tool_workspace(self, model_call_id: str | None) -> dict | None:
        row = self.store._conn.execute(
            """SELECT result_json, observation_json, result_digest FROM operations WHERE run_id=? AND tool_call_id=?
               AND state='result_committed'""", (self.run_id, model_call_id),
        ).fetchone()
        if row is None:
            return None
        if digest(encode([row["result_json"], row["observation_json"]])) != row["result_digest"]:
            raise StoreFormatError("stored tool result digest mismatch")
        return json.loads(row["observation_json"]).get("committed_workspace")

    def start_model(self, step: int, messages, tools, state: dict) -> str:
        call_id = uuid.uuid4().hex
        request_digest = digest(encode({
            "messages": [asdict(message) for message in messages],
            "tools": [asdict(tool) for tool in tools],
        }))
        with self.transaction() as db:
            self._checkpoint(db, step, "model_request", state, status="running")
            db.execute(
                """INSERT INTO model_calls (model_call_id, run_id, step_id, request_digest, status, created_at)
                   VALUES (?, ?, ?, ?, 'started', ?)""",
                (call_id, self.run_id, str(step), request_digest, time.time()),
            )
        return call_id

    def commit_model(self, call_id: str, step: int, response: LLMResponse, state: dict) -> None:
        action = {"action": response.action.to_dict(), "responses_call": response.responses_call}
        safe = redact_history_payload(action)
        if response.action.tool_call and response.action.tool_call.name == "memory_propose":
            safe = {"action": {"action_type": "tool_call", "tool_call": {"name": "memory_propose"}},
                    "responses_call": None}
        payload = encode({**safe, "redacted": safe != action})
        with self.transaction() as db:
            changed = db.execute(
                """UPDATE model_calls SET status='response_committed', action_json=?, usage_json=?,
                   finished_at=? WHERE model_call_id=? AND run_id=? AND step_id=? AND status='started'""",
                (payload, encode(asdict(response.usage)), time.time(), call_id, self.run_id, str(step)),
            )
            if changed.rowcount != 1:
                raise StoreFormatError("model response is not for a pending request")
            self._checkpoint(db, step, "model_response", {**state, "pending_model_call": call_id})

    def pending_response(self, checkpoint: dict) -> LLMResponse | None:
        call_id = checkpoint.get("pending_model_call")
        if not call_id:
            return None
        row = self.store._conn.execute(
            """SELECT action_json, usage_json FROM model_calls WHERE model_call_id=? AND run_id=?
               AND step_id=? AND status='response_committed'""",
            (call_id, self.run_id, str(checkpoint["next_step"])),
        ).fetchone()
        if row is None:
            raise StoreFormatError("committed model response is missing")
        payload = json.loads(row["action_json"])
        if payload["redacted"]:
            raise StoreFormatError("cannot restore a model action containing redacted fields")
        action = payload["action"]
        return LLMResponse(
            Action(ActionType(action["action_type"]), action["thought"],
                   ToolCall(**action["tool_call"]) if action["tool_call"] else None, action["message"]),
            "", TokenUsage(**json.loads(row["usage_json"])),
            responses_call=payload["responses_call"],
        )
