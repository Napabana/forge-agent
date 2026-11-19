"""Transfer verified memories into a fresh, isolated trial store."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from project_memory.runtime import MemoryRuntime
from project_memory.store import MemoryStore
from project_memory.writer import MemoryWriter


MAX_BYTES = 2_000_000
_TEXT = ("title", "type", "status", "confidence", "created_at", "updated_at", "content",
         "source_commit", "last_verified_commit")
_FIELDS = (*_TEXT, "memory_id", "source_files", "source_file_hashes", "source_run_ids", "tags")


def export_verified_memory(store: MemoryStore, repo_id: str) -> bytes:
    """Export from the trusted publisher's store, never directly from a trial."""
    with store.lock():
        rows = [{field: row.get(field) for field in _FIELDS} for row in store.read_records()
                if row.get("status") == "active" and row.get("confidence") == "verified"]
    rows.sort(key=lambda row: row["memory_id"])
    payload = json.dumps({"version": 1, "repo_id": repo_id, "records": rows},
                         ensure_ascii=False, sort_keys=True).encode()
    if len(payload) > MAX_BYTES:
        raise ValueError("memory bundle exceeds transfer size limit")
    return payload


def import_verified_memory(store: MemoryStore, repo_id: str, payload: bytes) -> int:
    """Hydrate an empty store for the expected repository before starting the trial."""
    if len(payload) > MAX_BYTES:
        raise ValueError("memory bundle exceeds transfer size limit")
    bundle = json.loads(payload)
    if not isinstance(bundle, dict) or bundle.get("version") != 1 or bundle.get("repo_id") != repo_id:
        raise ValueError("memory bundle repository or version mismatch")
    rows = bundle.get("records")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid memory bundle records")

    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(_FIELDS):
            raise ValueError("invalid transferred memory fields")
        record_id = row["memory_id"]
        if (not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{24}", record_id)
                or record_id in ids or row["type"] not in ("feature", "experience")
                or row["status"] != "active" or row["confidence"] != "verified"
                or any(not isinstance(row[key], str) for key in _TEXT)
                or any(not isinstance(row[key], list) or not all(isinstance(item, str) for item in row[key])
                       for key in ("source_files", "source_run_ids", "tags"))):
            raise ValueError("invalid or duplicate verified memory record")
        hashes = row["source_file_hashes"]
        if (not isinstance(hashes, dict) or hashes.keys() != set(row["source_files"])
                or any(not path or Path(path).is_absolute() or ".." in Path(path).parts
                       or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                       for path, digest in hashes.items())):
            raise ValueError("invalid transferred source-file evidence")
        ids.add(record_id)

    with store.lock():
        if any(item.name != ".lock" for item in store.memory.iterdir()):
            raise ValueError("memory transfer requires an empty trial store")
        for row in rows:
            meta = {key: value for key, value in row.items() if key != "content"}
            store.write(f"memory/{row['memory_id']}.md",
                        "---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n" + row["content"])
        MemoryWriter(store)._reindex()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    runtime = MemoryRuntime(args.repo, root=args.root, defer_promotion=True)
    with args.bundle.open("rb") as stream:
        print(import_verified_memory(runtime.store, args.repo_id, stream.read(MAX_BYTES + 1)))


if __name__ == "__main__":
    main()
