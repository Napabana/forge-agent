"""Single writer for candidates, formal memories and activity."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import yaml

from project_memory.store import MemoryStore, _KINDS, redact, source_hashes


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryWriter:
    def __init__(self, store: MemoryStore):
        self.store = store

    def propose(self, *, kind: str, title: str, content: str, related_files: list[str],
                tags: list[str], run_id: str, repo: str, session_id: str | None = None,
                trace_path: str | None = None, commit: str | None = None,
                supersedes: list[str] | None = None, isolate: bool = False) -> str:
        supersedes = supersedes or []
        if (not isinstance(kind, str) or kind not in _KINDS or not isinstance(title, str)
                or not isinstance(content, str) or not title.strip() or not content.strip()
                or not isinstance(related_files, list) or not isinstance(tags, list)
                or not isinstance(supersedes, list)
                or any(not isinstance(item, str) for item in related_files + tags + supersedes)
                or any(len(item) != 24 or any(c not in "0123456789abcdef" for c in item) for item in supersedes)):
            raise ValueError("invalid memory candidate")
        if len(title) > 200 or len(content) > 12000 or len(related_files) > 30 or len(tags) > 30:
            raise ValueError("memory candidate exceeds size limit")
        hashes = source_hashes(repo, related_files)
        if len(hashes) != len(set(related_files)):
            raise ValueError("candidate references missing or unsafe source files")
        candidate_id = hashlib.sha256(json.dumps([run_id, kind, title, content, sorted(related_files), sorted(supersedes)],
                                                ensure_ascii=False).encode()).hexdigest()[:24]
        payload = redact({"candidate_id": candidate_id, "type": kind, "title": title,
                          "content": content, "source_files": related_files,
                          "source_file_hashes": hashes, "tags": tags,
                          "source_run_ids": [run_id], "source_commit": commit,
                          "supersedes": supersedes,
                          "isolate": isolate,
                          "session_id": session_id, "trace_path": trace_path,
                          "created_at": now(), "status": "pending"})
        self.store.initialize()
        with self.store.lock():
            relative = f"memory/pending/{candidate_id}.json"
            if not self.store.path(relative).exists():
                self.store.write(relative, json.dumps(payload, ensure_ascii=False, indent=2))
        return candidate_id

    def pending(self) -> list[dict]:
        directory = self.store.path("memory/pending")
        rows = []
        for path in directory.glob("*.json") if directory.exists() else ():
            try:
                self.store.path(str(path.relative_to(self.store.base)))
                row = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(row, dict) and row.get("candidate_id") == path.stem:
                    rows.append(row)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return rows

    def resolve(self, candidate_id: str, *, status: str, repo: str,
                verified: bool = False) -> dict:
        if not candidate_id or not all(c in "0123456789abcdef" for c in candidate_id) or len(candidate_id) != 24:
            raise ValueError("invalid candidate id")
        if status not in {"approved", "rejected", "consolidated"}:
            raise ValueError("invalid candidate resolution")
        with self.store.lock():
            path = self.store.path(f"memory/pending/{candidate_id}.json")
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("candidate_id") != candidate_id or row.get("status") != "pending":
                raise ValueError("candidate is not pending")
            if status == "approved" and source_hashes(repo, row.get("source_files", [])) != row.get("source_file_hashes", {}):
                raise ValueError("candidate source files changed or are absent; verify current repo first")
            if status != "rejected":
                confidence = "verified" if verified else "user_confirmed" if status == "approved" else "proposed"
                record = {"memory_id": candidate_id, "type": row["type"], "title": row["title"],
                          "status": "active" if confidence != "proposed" else "proposed",
                          "confidence": confidence, "created_at": row["created_at"], "updated_at": now(),
                          "source_run_ids": row["source_run_ids"], "source_commit": row.get("source_commit"),
                          "source_files": row["source_files"], "source_file_hashes": row["source_file_hashes"],
                          "last_verified_commit": row.get("source_commit") if verified else None,
                          "tags": row["tags"]}
                if row.get("supersedes"):
                    record["supersedes"] = row["supersedes"]
                category = {"feature": "features", "decision": "decisions", "experience": "experiences",
                            "workflow": "workflows"}.get(row["type"])
                relative = f"memory/{category + '/' if category else ''}{candidate_id}.md"
                self.store.write(relative, "---\n" + yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
                                 + "---\n" + row["content"].rstrip() + "\n")
                for old in self.store.read_records():
                    if old["memory_id"] not in row.get("supersedes", []):
                        continue
                    old["status"] = "superseded"
                    old["superseded_by"] = candidate_id
                    content = old.pop("content")
                    old_path = old.pop("path")
                    self.store.write(old_path, "---\n" + yaml.safe_dump(old, allow_unicode=True, sort_keys=False)
                                     + "---\n" + content.rstrip() + "\n")
                self._reindex()
            row["status"] = status
            row["resolved_at"] = now()
            self.store.write(f"memory/pending/{candidate_id}.json", json.dumps(row, ensure_ascii=False, indent=2))
            return row

    def append_activity(self, row: dict) -> None:
        self.store.initialize()
        with self.store.lock():
            path = self.store.path("memory/activity.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(redact(row), ensure_ascii=False) + "\n")
                handle.flush()
                import os
                os.fsync(handle.fileno())

    def _reindex(self) -> None:
        lines = ["# Project Memory", ""]
        for row in sorted((row for row in self.store.read_records() if row.get("status") == "active"),
                          key=lambda r: r["title"]):
            lines.append(f"- {row['title']} ({row['type']}, {row['confidence']}) — {row['memory_id']}")
        self.store.write("memory/MEMORY.md", "\n".join(lines) + "\n")
