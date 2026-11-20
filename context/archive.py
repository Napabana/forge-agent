"""Immutable context records and checked projections within one Run scope."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from durable_runtime.run_manager import digest, encode, redact_history_payload
from durable_runtime.store import StoreFormatError

MAX_ARCHIVE_BUNDLE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class DerivedArchive:
    kind: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveRewrite:
    expected_revision: int
    previous: tuple[str, ...]
    messages: tuple[dict, ...]
    mapping: tuple[str | DerivedArchive, ...]


class ContextArchive:
    def __init__(self, store, scope_id: str, inline_limit: int = 65_536, *,
                 run_id: str | None = None, session_id: str | None = None):
        self.store = store
        self.scope_id = scope_id
        self.inline_limit = inline_limit
        self.run_id = run_id
        self.session_id = session_id

    def records(self) -> list[dict]:
        return [dict(row) for row in self.store._conn.execute(
            """SELECT archive_id, sequence, kind, payload_digest, source_ids_json, origin_run_id, origin_session_id
               FROM archive_records WHERE scope_id=? ORDER BY sequence""", (self.scope_id,),
        )]

    def _blob(self, checksum: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise StoreFormatError("invalid archive Blob digest")
        return self.store.blobs / checksum

    def _put_blob(self, payload: str, checksum: str) -> str:
        target = self._blob(checksum)
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload.encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            if os.name != "nt":
                folder = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return checksum

    def _record(self, db, archive_id: str):
        row = db.execute(
            "SELECT * FROM archive_records WHERE scope_id=? AND archive_id=?", (self.scope_id, archive_id),
        ).fetchone()
        if row is None:
            raise StoreFormatError("archive record missing from current scope")
        return row

    def _payload(self, row) -> str:
        payload = row["payload_json"]
        if row["blob_ref"] is not None:
            if row["blob_ref"] != row["payload_digest"]:
                raise StoreFormatError("archive Blob reference mismatch")
            path = self._blob(row["blob_ref"])
            if path.is_symlink() or not path.is_file():
                raise StoreFormatError("archive Blob missing or invalid")
            try:
                payload = path.read_bytes().decode("utf-8")
            except (UnicodeError, OSError) as exc:
                raise StoreFormatError("archive Blob cannot be read") from exc
        if payload is None or digest(payload) != row["payload_digest"]:
            raise StoreFormatError("archive payload digest mismatch")
        return payload

    def _append(self, db, kind, value, source_ids=()) -> str:
        if kind not in {"raw", "summary", "prune", "boundary", "import"}:
            raise ValueError("invalid archive record kind")
        if kind in {"summary", "prune"} and not source_ids:
            raise ValueError("derived archive records need sources")
        for source in source_ids:
            self._payload(self._record(db, source))
        row = db.execute(
            "SELECT archive_id, sequence FROM archive_records WHERE scope_id=? ORDER BY sequence DESC LIMIT 1",
            (self.scope_id,),
        ).fetchone()
        sequence = row["sequence"] + 1 if row else 1
        archive_id = uuid.uuid4().hex
        payload = encode(redact_history_payload(value))
        checksum = digest(payload)
        blob = self._put_blob(payload, checksum) if len(payload.encode("utf-8")) > self.inline_limit else None
        db.execute(
            """INSERT INTO archive_records (archive_id, scope_id, sequence, kind, payload_json,
               blob_ref, payload_digest, source_ids_json, previous_id, redaction_version, created_at,
               origin_run_id, origin_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (archive_id, self.scope_id, sequence, kind, None if blob else payload, blob,
             checksum, encode(list(source_ids)), row["archive_id"] if row else None, time.time(),
             self.run_id, self.session_id),
        )
        return archive_id

    def _verify_closure(self, db, row) -> None:
        pending = [row]
        visited = set()
        while pending:
            parent = pending.pop()
            if parent["archive_id"] in visited:
                continue
            visited.add(parent["archive_id"])
            self._payload(parent)
            sources = json.loads(parent["source_ids_json"])
            if parent["kind"] in {"summary", "prune"} and not sources:
                raise StoreFormatError("derived archive record has no source")
            for archive_id in sources:
                child = self._record(db, archive_id)
                if child["sequence"] >= parent["sequence"]:
                    raise StoreFormatError("archive source is circular or out of order")
                pending.append(child)

    def _project(self, db, kind: str, lineage: list[str], expected_revision: int) -> int:
        if kind not in {"canonical", "model_view"}:
            raise ValueError("invalid archive projection")
        current, _ = self.projection(db, kind)
        if current != expected_revision:
            raise StoreFormatError("archive projection revision differs")
        content = [json.loads(self._payload(self._record(db, item))) for item in lineage]
        revision = current + 1
        db.execute(
            """INSERT INTO archive_projections VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope_id, projection_kind) DO UPDATE SET revision=excluded.revision,
               lineage_json=excluded.lineage_json, messages_digest=excluded.messages_digest,
               updated_at=excluded.updated_at""",
            (self.scope_id, kind, revision, encode(lineage), digest(encode(content)), time.time()),
        )
        return revision

    def projection(self, db, kind: str) -> tuple[int, list[str]]:
        row = db.execute(
            "SELECT * FROM archive_projections WHERE scope_id=? AND projection_kind=?", (self.scope_id, kind),
        ).fetchone()
        if row is None:
            return 0, []
        ids = json.loads(row["lineage_json"])
        content = [json.loads(self._payload(self._record(db, item))) for item in ids]
        if digest(encode(content)) != row["messages_digest"]:
            raise StoreFormatError("archive projection digest mismatch")
        return row["revision"], ids

    def capture_history(self, db, messages: list[dict]) -> int:
        revision, existing = self.projection(db, "canonical")
        prefix = []
        for archive_id, message in zip(existing, messages):
            if self._record(db, archive_id)["payload_digest"] != digest(encode(redact_history_payload(message))):
                break
            prefix.append(archive_id)
        if len(prefix) < len(existing):
            self._append(db, "boundary", {"reason": "canonical history replaced"}, existing[len(prefix):])
        lineage = prefix + [self._append(db, "raw", message) for message in messages[len(prefix):]]
        if lineage == existing:
            return revision
        return self._project(db, "canonical", lineage, revision)

    def apply_rewrite(self, db, rewrite: ArchiveRewrite) -> int:
        revision, previous = self.projection(db, "model_view")
        if revision != rewrite.expected_revision or tuple(previous) != rewrite.previous:
            raise StoreFormatError("archive rewrite precondition differs")
        if len(rewrite.messages) != len(rewrite.mapping):
            raise StoreFormatError("archive rewrite needs one source mapping per message")
        lineage = []
        for message, mapping in zip(rewrite.messages, rewrite.mapping, strict=True):
            checksum = digest(encode(redact_history_payload(message)))
            if isinstance(mapping, str):
                row = self._record(db, mapping)
                self._verify_closure(db, row)
                if row["payload_digest"] != checksum:
                    raise StoreFormatError("archive rewrite changes a retained source")
                lineage.append(mapping)
                continue
            if mapping.kind not in {"summary", "prune", "boundary", "import"}:
                raise StoreFormatError("invalid derived archive kind")
            for source in mapping.sources:
                self._verify_closure(db, self._record(db, source))
            prior = db.execute(
                """SELECT archive_id FROM archive_records WHERE scope_id=? AND kind=?
                   AND payload_digest=? AND source_ids_json=? ORDER BY sequence DESC LIMIT 1""",
                (self.scope_id, mapping.kind, checksum, encode(list(mapping.sources))),
            ).fetchone()
            if prior:
                self._verify_closure(db, self._record(db, prior[0]))
            lineage.append(prior[0] if prior else self._append(db, mapping.kind, message, mapping.sources))
        return self._project(db, "model_view", lineage, revision)

    def rewrite_model_view(self, db, messages: list[dict], expected_revision: int, summary_prefix: int | None = None) -> int:
        """Build a source mapping from canonical history before rewriting the model view."""
        _, canonical = self.projection(db, "canonical")
        _, previous = self.projection(db, "model_view")
        raw = [(archive_id, json.loads(self._payload(self._record(db, archive_id)))) for archive_id in canonical]
        by_digest = defaultdict(deque)
        for archive_id, message in raw:
            by_digest[digest(encode(message))].append(archive_id)
        by_event = {message["event_ref"]: archive_id for archive_id, message in raw if message.get("event_ref")}
        messages = tuple(redact_history_payload(messages))
        mapping = []
        for message in messages:
            existing = by_digest.get(digest(encode(message)))
            if existing:
                mapping.append(existing.popleft())
                continue
            source = by_event.get(message.get("event_ref"))
            mapping.append(DerivedArchive("prune", (source,)) if source else
                           DerivedArchive("summary", tuple(canonical[:summary_prefix])))
        return self.apply_rewrite(db, ArchiveRewrite(expected_revision, tuple(previous), messages, tuple(mapping)))

    def import_history(self, db, messages: list[dict], session_id: str) -> str:
        checksum = digest(encode(redact_history_payload(messages)))
        for row in db.execute(
            "SELECT * FROM archive_records WHERE scope_id=? AND kind='import' ORDER BY sequence",
            (self.scope_id,),
        ):
            self._verify_closure(db, row)
            if json.loads(self._payload(row)) == {"session_id": session_id, "history_digest": checksum}:
                return row["archive_id"]
        if self.projection(db, "canonical")[0] or self.projection(db, "model_view")[0]:
            raise StoreFormatError("cannot import a different history into an initialized scope")
        self.capture_history(db, messages)
        _, sources = self.projection(db, "canonical")
        record = self._append(db, "import", {"session_id": session_id, "history_digest": checksum}, sources)
        self.apply_rewrite(db, ArchiveRewrite(0, (), tuple(messages), tuple(sources)))
        return record

    def export_bundle(self, archive_ids: tuple[str, ...]) -> dict:
        db = self.store._conn
        if not archive_ids:
            _, active = self.projection(db, "model_view")
            _, canonical = self.projection(db, "canonical")
            archive_ids = tuple(dict.fromkeys(active + canonical))
        if not archive_ids:
            raise StoreFormatError("source scope has no records to export")
        rows = {}
        pending = list(archive_ids)
        while pending:
            archive_id = pending.pop()
            if archive_id in rows:
                continue
            row = self._record(db, archive_id)
            sources = json.loads(row["source_ids_json"])
            rows[archive_id] = (row["sequence"], {
                "id": archive_id, "kind": row["kind"], "sources": sources,
                "payload": json.loads(self._payload(row)), "digest": row["payload_digest"],
                "run_id": row["origin_run_id"], "session_id": row["origin_session_id"],
            })
            pending.extend(sources)
        value = {
            "version": 1, "project": self.store.identity.project_id, "source_scope": self.scope_id,
            "roots": list(dict.fromkeys(archive_ids)), "records": [entry for _, entry in sorted(rows.values())],
        }
        self._validate_bundle(value)
        return {**value, "digest": digest(encode(value))}

    def _validate_bundle(self, bundle: dict) -> None:
        if (type(bundle) is not dict or set(bundle) != {"version", "project", "source_scope", "roots", "records"}
                or type(bundle["version"]) is not int or bundle["version"] != 1
                or bundle["project"] != self.store.identity.project_id
                or not isinstance(bundle["source_scope"], str) or not bundle["source_scope"]
                or not isinstance(bundle["roots"], list) or not bundle["roots"]
                or not isinstance(bundle["records"], list) or not bundle["records"]):
            raise StoreFormatError("invalid archive bundle or project mismatch")
        records = {}
        for record in bundle["records"]:
            if (type(record) is not dict
                    or set(record) != {"id", "kind", "sources", "payload", "digest", "run_id", "session_id"}
                    or not isinstance(record["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["id"])
                    or record["id"] in records or record["kind"] not in ("raw", "summary", "prune", "boundary", "import")
                    or not isinstance(record["sources"], list)
                    or any(not isinstance(item, str) or item not in records for item in record["sources"])
                    or (record["kind"] in {"summary", "prune"} and not record["sources"])
                    or any(record[key] is not None and not isinstance(record[key], str) for key in ("run_id", "session_id"))
                    or redact_history_payload(record["payload"]) != record["payload"]
                    or digest(encode(record["payload"])) != record["digest"]):
                raise StoreFormatError("archive bundle record, source order, or digest differs")
            records[record["id"]] = record["sources"]
        if any(not isinstance(item, str) or item not in records for item in bundle["roots"]):
            raise StoreFormatError("archive bundle root is missing")
        pending, closure = list(bundle["roots"]), set()
        while pending:
            key = pending.pop()
            if key not in closure:
                closure.add(key)
                pending.extend(records[key])
        if closure != records.keys():
            raise StoreFormatError("archive bundle contains records outside its declared roots")

    def import_bundle(self, db, package: dict) -> dict:
        if type(package) is not dict or "digest" not in package:
            raise StoreFormatError("archive bundle digest is missing")
        value = {key: item for key, item in package.items() if key != "digest"}
        checksum = digest(encode(value))
        if checksum != package["digest"]:
            raise StoreFormatError("archive bundle digest differs")
        self._validate_bundle(value)
        if value["source_scope"] == self.scope_id:
            raise StoreFormatError("archive bundle requires a different destination scope")
        for row in db.execute(
            "SELECT * FROM archive_records WHERE scope_id=? AND kind='import' ORDER BY sequence", (self.scope_id,),
        ):
            marker = json.loads(self._payload(row))
            if isinstance(marker, dict) and marker.get("bundle_digest") == checksum:
                self._verify_closure(db, row)
                return {"archive_id": row["archive_id"], "roots": json.loads(row["source_ids_json"])}
        ids = {}
        for record in value["records"]:
            ids[record["id"]] = self._append(db, record["kind"], record["payload"],
                                             [ids[item] for item in record["sources"]])
        sources = [ids[key] for key in value["roots"]]
        marker = self._append(db, "import", {
            "bundle_digest": checksum, "source_scope": value["source_scope"],
            "origins": {ids[row["id"]]: {"archive_id": row["id"], "run_id": row["run_id"],
                                        "session_id": row["session_id"]} for row in value["records"]},
        }, sources)
        return {"archive_id": marker, "roots": sources}

    def model_archive_ids(self) -> list[str | None]:
        db = self.store._conn
        _, ids = self.projection(db, "model_view")
        return [item if self._record(db, item)["kind"] in {"summary", "prune"} else None for item in ids]

    def restore_model_view(self, expected_revision: int) -> list[dict]:
        revision, ids = self.verified_projection("model_view")
        if revision != expected_revision:
            raise StoreFormatError("saved model view revision differs")
        db = self.store._conn
        return [json.loads(self._payload(self._record(db, item))) for item in ids]

    def clear(self, db) -> None:
        canonical_revision, existing = self.projection(db, "canonical")
        model_revision, _ = self.projection(db, "model_view")
        boundary = self._append(db, "boundary", {"reason": "chat clear"}, existing)
        self._project(db, "canonical", [], canonical_revision)
        _, previous = self.projection(db, "model_view")
        self.apply_rewrite(db, ArchiveRewrite(model_revision, tuple(previous), ({"reason": "chat clear"},), (boundary,)))

    def verified_projection(self, kind: str) -> tuple[int, list[str]]:
        db = self.store._conn
        revision, ids = self.projection(db, kind)
        for archive_id in ids:
            self._verify_closure(db, self._record(db, archive_id))
        return revision, ids

    def read(self, archive_id: str, offset: int, limit: int) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", archive_id):
            raise ValueError("invalid archive ID")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16_000:
            raise ValueError("invalid archive page")
        row = self._record(self.store._conn, archive_id)
        self._verify_closure(self.store._conn, row)
        payload = self._payload(row)
        end = min(offset + limit, len(payload))
        return {"archive_id": archive_id, "kind": row["kind"], "offset": offset,
                "total_chars": len(payload), "next_offset": end if end < len(payload) else None,
                "has_more": end < len(payload), "content": payload[offset:end],
                "payload_digest": row["payload_digest"], "source_ids": json.loads(row["source_ids_json"])}
