"""Repository change detection used by Repo Map and completion guards."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.loop_detector import snapshot_repository


_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
    ".pytest_cache", "dist", "build",
})


@dataclass(frozen=True)
class RepositoryState:
    kind: str
    head: str = ""
    status_paths: tuple[str, ...] = ()
    renames: tuple[tuple[str, str], ...] = ()
    fingerprint: str = ""

    @property
    def is_git(self) -> bool:
        return self.kind == "git"

    def to_json(self) -> str:
        return json.dumps({
            "kind": self.kind,
            "head": self.head,
            "status_paths": list(self.status_paths),
            "renames": [list(item) for item in self.renames],
            "fingerprint": self.fingerprint,
        }, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> "RepositoryState | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                kind=str(data["kind"]),
                head=str(data.get("head", "")),
                status_paths=tuple(str(item) for item in data.get("status_paths", ())),
                renames=tuple((str(item[0]), str(item[1])) for item in data.get("renames", ())),
                fingerprint=str(data.get("fingerprint", "")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


@dataclass(frozen=True)
class RepositoryChanges:
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    renames: tuple[tuple[str, str], ...] = ()
    full_rebuild: bool = False
    reason: str = ""

    @property
    def all_paths(self) -> tuple[str, ...]:
        values = set(self.changed_paths) | set(self.deleted_paths)
        for old, new in self.renames:
            values.add(old)
            values.add(new)
        return tuple(sorted(values))


def capture_repository_state(repo_path: str | Path) -> RepositoryState:
    root = Path(repo_path).resolve()
    head = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if head is not None and status is not None:
        paths, renames = _parse_status_porcelain_z(status)
        raw = head + "\0" + status + "\0" + _paths_content_fingerprint(root, paths)
        return RepositoryState(
            kind="git",
            head=head.strip(),
            status_paths=tuple(sorted(paths)),
            renames=tuple(sorted(renames)),
            fingerprint=hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        )
    return RepositoryState(kind="filesystem", fingerprint=_filesystem_fingerprint(root))


def detect_repository_changes(
    repo_path: str | Path,
    previous: RepositoryState | None,
    current: RepositoryState,
) -> RepositoryChanges:
    root = Path(repo_path).resolve()
    if previous is None:
        return RepositoryChanges(full_rebuild=True, reason="missing_previous_state")
    if previous.kind != current.kind:
        return RepositoryChanges(full_rebuild=True, reason="repository_kind_changed")
    if not current.is_git:
        if previous.fingerprint == current.fingerprint:
            return RepositoryChanges()
        return RepositoryChanges(full_rebuild=True, reason="non_git_repository_changed")

    changed = set(previous.status_paths) | set(current.status_paths)
    deleted: set[str] = set()
    renames = set(previous.renames) | set(current.renames)
    if previous.head != current.head:
        diff = _git(root, "diff", "--name-status", "-z", "--find-renames", previous.head, current.head, "--")
        if diff is None:
            return RepositoryChanges(full_rebuild=True, reason="head_diff_unavailable")
        try:
            diff_changed, diff_deleted, diff_renames = _parse_name_status_z(diff)
        except ValueError:
            return RepositoryChanges(full_rebuild=True, reason="head_diff_unparseable")
        changed.update(diff_changed)
        deleted.update(diff_deleted)
        renames.update(diff_renames)

    for old, new in renames:
        changed.add(old)
        changed.add(new)
    for path in list(changed):
        if not (root / path).exists():
            deleted.add(path)
    return RepositoryChanges(
        changed_paths=tuple(sorted(changed - deleted)),
        deleted_paths=tuple(sorted(deleted)),
        renames=tuple(sorted(renames)),
        reason="git_delta" if changed or deleted or renames else "unchanged",
    )


def repository_fingerprint(repo_path: str | Path) -> str:
    """Return the frozen HEAD + working-tree fingerprint used outside Repo Map.

    Repo Map needs richer change-detection state than the pre-existing Context/
    Chat/completion contract. Keep that richer state in ``capture_repository_state``
    instead of changing this public helper's semantics.
    """
    root = Path(repo_path)
    head = "no-head"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            head = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{head}:{snapshot_repository(root)}"


def repository_content_fingerprint(repo_path: str | Path) -> str:
    """Return a checkout-content fingerprint that ignores Git metadata-only changes.

    This is intentionally distinct from repository_fingerprint. Completion
    verification needs to know whether the files a test observed have changed;
    staging or committing those same bytes must not invalidate a successful test.
    """
    root = Path(repo_path).resolve()
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is None:
        return _filesystem_fingerprint(root)

    digest = hashlib.sha256()
    for relative in sorted(path for path in listed.split("\0") if path):
        normalized = relative.replace("\\", "/")
        target = root / relative
        digest.update(normalized.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        if target.is_symlink():
            digest.update(b"symlink\0")
            try:
                digest.update(str(target.readlink()).encode("utf-8", errors="replace"))
            except OSError:
                digest.update(b"<unreadable>")
        elif target.is_file():
            digest.update(b"file\0")
            try:
                digest.update(b"x" if target.stat().st_mode & 0o111 else b"-")
                with target.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            except OSError:
                digest.update(b"<unreadable>")
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_status_porcelain_z(raw: str) -> tuple[set[str], set[tuple[str, str]]]:
    items = raw.split("\0")
    paths: set[str] = set()
    renames: set[tuple[str, str]] = set()
    i = 0
    while i < len(items):
        record = items[i]
        i += 1
        if not record:
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        path = record[3:]
        paths.add(path)
        if "R" in status or "C" in status:
            if i >= len(items) or not items[i]:
                continue
            old_path = items[i]
            i += 1
            paths.add(old_path)
            renames.add((old_path, path))
    return paths, renames


def _parse_name_status_z(raw: str) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    items = raw.split("\0")
    changed: set[str] = set()
    deleted: set[str] = set()
    renames: set[tuple[str, str]] = set()
    i = 0
    while i < len(items):
        status = items[i]
        i += 1
        if not status:
            continue
        if i >= len(items):
            raise ValueError("missing path")
        if status.startswith(("R", "C")):
            old_path = items[i]
            i += 1
            if i >= len(items):
                raise ValueError("missing rename destination")
            new_path = items[i]
            i += 1
            changed.update((old_path, new_path))
            renames.add((old_path, new_path))
        else:
            path = items[i]
            i += 1
            if status.startswith("D"):
                deleted.add(path)
            else:
                changed.add(path)
    return changed, deleted, renames


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _paths_content_fingerprint(root: Path, paths: set[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        digest.update(relative.encode("utf-8", errors="replace"))
        path = root / relative
        if not path.is_file():
            digest.update(b"<missing>")
            continue
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _filesystem_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"missing")
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts) or not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8", errors="replace"))
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()
