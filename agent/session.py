"""Durable state models for interactive chat sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from llm.usage import SessionUsage


SESSION_STATE_VERSION = 4


class ChatSessionError(RuntimeError):
    """Base error for durable chat sessions."""


class ChatSessionNotFound(ChatSessionError):
    """Raised when a requested session does not exist."""


class ChatSessionRepoMismatch(ChatSessionError):
    """Raised when a session belongs to another repository."""


class ChatSessionFormatError(ChatSessionError):
    """Raised when persisted session data cannot be read safely."""


class ChatSessionConflict(ChatSessionError):
    """Raised when saving a stale copy of a chat session."""


@dataclass
class PendingRoundState:
    round_number: int
    task_id: str
    user_input: str
    started_at: str
    log_path: str
    run_id: str | None = None
    scope_id: str | None = None
    runtime_revision: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingRoundState":
        return cls(**data)


@dataclass
class ChatRoundState:
    round_number: int
    task_id: str
    user_input: str
    status: str
    log_path: str
    started_at: str
    summary: str = ""
    steps: int = 0
    tokens: int = 0
    usage: SessionUsage = field(default_factory=SessionUsage)
    finished_at: str | None = None
    error: str | None = None
    run_id: str | None = None
    history_digest: str | None = None
    origin_session_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatRoundState":
        raw = dict(data)
        if "usage" in raw:
            raw["usage"] = SessionUsage.from_dict(raw["usage"])
        else:
            raw["usage"] = SessionUsage(
                unattributed_tokens=int(raw.get("tokens", 0))
            )
        return cls(**raw)


@dataclass
class ChatSessionState:
    session_id: str
    repo_path: str
    repo_key: str
    created_at: str
    updated_at: str
    title: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    round_count: int = 0
    total_steps: int = 0
    total_tokens: int = 0
    usage: SessionUsage = field(default_factory=SessionUsage)
    repo_revision: str = ""
    pending_round: PendingRoundState | None = None
    rounds: list[ChatRoundState] = field(default_factory=list)
    compaction_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0
    forked_from_session_id: str | None = None
    forked_from_round: int | None = None
    version: int = SESSION_STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatSessionState":
        version = data.get("version")
        if version not in {1, 2, 3, SESSION_STATE_VERSION}:
            raise ChatSessionFormatError(
                f"unsupported chat session version: {version!r}"
            )
        try:
            raw = dict(data)
            raw["version"] = SESSION_STATE_VERSION
            if version == 1:
                raw.setdefault("revision", 0)
                raw.setdefault("compaction_checkpoints", [])
            pending = raw.get("pending_round")
            raw["pending_round"] = (
                PendingRoundState.from_dict(pending) if pending else None
            )
            raw["rounds"] = [
                ChatRoundState.from_dict(item) for item in raw.get("rounds", [])
            ]
            if "usage" in raw:
                raw["usage"] = SessionUsage.from_dict(raw["usage"])
            else:
                raw["usage"] = SessionUsage(
                    unattributed_tokens=int(raw.get("total_tokens", 0))
                )
            return cls(**raw)
        except (TypeError, KeyError, AttributeError) as exc:
            raise ChatSessionFormatError(f"invalid chat session state: {exc}") from exc
