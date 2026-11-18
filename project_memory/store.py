"""Validated, atomic project memory files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

from project_memory.identity import ProjectIdentity

_SECRET = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\s*([:=])\s*[^\s,;]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
)
_KINDS = {"project", "architecture", "feature", "decision", "experience", "constraint", "workflow"}


def redact(value):
    if isinstance(value, str):
        for pattern, replacement in _SECRET:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class MemoryStore:
    def __init__(self, identity: ProjectIdentity, root: str | Path | None = None):
        self.identity = identity
        self.base = (Path(root).expanduser() if root else Path.home() / ".forge-agent" / "projects") / identity.project_id
        self.memory = self.base / "memory"

    def path(self, relative: str) -> Path:
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("unsafe memory path")
        target = self.base / relative
        if not target.resolve().is_relative_to(self.base.resolve()):
            raise ValueError("memory path leaves project store")
        return target

    @contextmanager
    def lock(self):
        self.memory.mkdir(parents=True, exist_ok=True)
        for directory in (self.base, self.memory):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        lock = self.path("memory/.lock")
        with lock.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def write(self, relative: str, content: str) -> None:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".memory-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(redact(content))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            try:
                directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def initialize(self) -> None:
        with self.lock():
            identity_path = self.path("identity.json")
            if not identity_path.exists():
                self.write("identity.json", json.dumps({"project_id": self.identity.project_id,
                    "source": self.identity.source, "key": self.identity.key}, ensure_ascii=False))
            index = self.path("memory/MEMORY.md")
            if not index.exists():
                self.write("memory/MEMORY.md", "# Project Memory\n\n")

    def read_records(self) -> list[dict]:
        records = []
        for path in self.memory.rglob("*.md") if self.memory.exists() else ():
            if path.name == "MEMORY.md":
                continue
            try:
                safe = self.path(str(path.relative_to(self.base)))
                content = safe.read_text(encoding="utf-8")
                if not content.startswith("---\n"):
                    continue
                header, body = content[4:].split("\n---\n", 1)
                metadata = yaml.safe_load(header)
                if isinstance(metadata, dict) and metadata.get("memory_id"):
                    records.append({**metadata, "content": body, "path": str(path.relative_to(self.base))})
            except (OSError, ValueError, yaml.YAMLError):
                continue
        return records

    def read_activity(self) -> list[dict]:
        path = self.path("memory/activity.jsonl")
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except json.JSONDecodeError:
                continue
        return rows


def safe_source_file(repo: str | Path, relative: str) -> Path | None:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    root = Path(repo).resolve()
    path = (root / relative).resolve()
    if (not path.is_relative_to(root) or not path.is_file()
            or path.name.startswith(".env") or path.name in {"id_rsa", "id_ed25519"}
            or path.suffix in {".pem", ".key", ".p12"}):
        return None
    return path


def source_hashes(repo: str | Path, files: list[str]) -> dict[str, str]:
    result = {}
    for name in files:
        path = safe_source_file(repo, name)
        if path is not None:
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
