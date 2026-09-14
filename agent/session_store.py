"""Atomic JSON persistence for interactive chat sessions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent.session import (
    ChatSessionFormatError,
    ChatSessionNotFound,
    ChatSessionState,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_key_for_path(repo_path: str | Path) -> str:
    """Return a stable repository identity shared by Git worktrees."""
    root = Path(repo_path).resolve()
    identity = str(root)
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        common_dir = proc.stdout.strip()
        if proc.returncode == 0 and common_dir:
            common_path = Path(common_dir)
            if not common_path.is_absolute():
                common_path = root / common_path
            identity = str(common_path.resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


class ChatSessionStore(Protocol):
    def create(self, repo_path: str | Path) -> ChatSessionState: ...
    def save(self, state: ChatSessionState) -> None: ...
    def load(self, session_id: str) -> ChatSessionState: ...
    def latest_for_repo(self, repo_path: str | Path) -> ChatSessionState | None: ...
    def round_log_dir(self, state: ChatSessionState) -> Path: ...
    def state_path(self, state: ChatSessionState) -> Path: ...


class JsonChatSessionStore:
    """One atomic state snapshot plus per-round EventLogs for each session."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def create(self, repo_path: str | Path) -> ChatSessionState:
        now = utc_now()
        state = ChatSessionState(
            session_id=uuid.uuid4().hex[:12],
            repo_path=str(Path(repo_path).resolve()),
            repo_key=repo_key_for_path(repo_path),
            created_at=now,
            updated_at=now,
        )
        self.save(state)
        return state

    def state_path(self, state: ChatSessionState) -> Path:
        return self.root / state.repo_key / state.session_id / "state.json"

    def round_log_dir(self, state: ChatSessionState) -> Path:
        return self.state_path(state).parent / "rounds"

    def save(self, state: ChatSessionState) -> None:
        state.updated_at = utc_now()
        path = self.state_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(state.to_dict(), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(tmp_path, path)
            self._fsync_directory(path.parent)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def load(self, session_id: str) -> ChatSessionState:
        matches = list(self.root.glob(f"*/{session_id}/state.json"))
        if not matches:
            raise ChatSessionNotFound(f"chat session not found: {session_id}")
        if len(matches) > 1:
            raise ChatSessionFormatError(
                f"chat session id is ambiguous: {session_id}"
            )
        return self._load_path(matches[0])

    def latest_for_repo(self, repo_path: str | Path) -> ChatSessionState | None:
        repo_dir = self.root / repo_key_for_path(repo_path)
        states = [self._load_path(path) for path in repo_dir.glob("*/state.json")]
        return max(states, key=lambda state: state.updated_at, default=None)

    def _load_path(self, path: Path) -> ChatSessionState:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ChatSessionFormatError(
                f"cannot read chat session {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ChatSessionFormatError(f"invalid chat session state: {path}")
        return ChatSessionState.from_dict(raw)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
