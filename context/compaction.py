"""Deterministic, traceable history compaction for prepare_next_turn."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from agent.core import PrepareNextTurnContext
from agent.task import EventType
from context.token_budget import (
    estimate_messages_tokens,
    history_unit_tokens,
    recent_history_units,
)
from llm.base import LLMMessage


@dataclass(frozen=True)
class CompactionCheckpoint:
    checkpoint_id: str
    created_at: str
    source_event_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    repo_revision: str
    summary_method: str
    summary_hash: str
    retained_tail: int
    keep_recent_tokens: int
    retained_tail_tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


class TraceableCompaction:
    """Compact old history only when its assigned prompt budget is pressured."""

    def __init__(
        self,
        *,
        threshold: float = 0.8,
        target_ratio: float = 0.5,
        keep_recent_tokens: int = 8_000,
        max_summary_chars: int = 2_000,
    ) -> None:
        if not 0 < target_ratio < threshold <= 1:
            raise ValueError("require 0 < target_ratio < threshold <= 1")
        if keep_recent_tokens <= 0:
            raise ValueError("keep_recent_tokens must be positive")
        self.threshold = threshold
        self.target_ratio = target_ratio
        self.keep_recent_tokens = keep_recent_tokens
        self.max_summary_chars = max_summary_chars
        self.checkpoints: list[CompactionCheckpoint] = []

    def __call__(self, context: PrepareNextTurnContext):
        messages = context.history.to_list()
        raw = context.history.to_dicts()
        before_tokens = estimate_messages_tokens(raw)
        history_budget = context.token_budget.default_plan().history
        if len(messages) <= 1 or before_tokens < history_budget * self.threshold:
            return None

        recent_units = recent_history_units(raw, self.keep_recent_tokens)
        if not recent_units:
            return None
        tail_start = recent_units[0].indices[0]
        dropped = messages[1:tail_start]
        if not dropped:
            return None

        summary = self._summary(
            dropped,
            min(self.max_summary_chars, int(history_budget * self.target_ratio * 4)),
        )
        compacted = [
            messages[0],
            LLMMessage(
                role="user",
                content="[Compacted earlier context; full tool results remain in EventLog]\n" + summary,
            ),
            *messages[tail_start:],
        ]
        context.history.replace(compacted)
        after_tokens = estimate_messages_tokens(context.history.to_dicts())
        source_event_ids = tuple(
            message.event_ref for message in dropped if message.event_ref is not None
        )
        retained_tail_tokens = sum(
            history_unit_tokens(raw, unit) for unit in recent_units
        )
        checkpoint = CompactionCheckpoint(
            checkpoint_id=uuid.uuid4().hex[:12],
            created_at=datetime.now(timezone.utc).isoformat(),
            source_event_ids=source_event_ids,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            repo_revision=_repo_revision(context.task.repo_path),
            summary_method="extractive-v1",
            summary_hash=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            retained_tail=len(messages) - tail_start,
            keep_recent_tokens=self.keep_recent_tokens,
            retained_tail_tokens=retained_tail_tokens,
        )
        self.checkpoints.append(checkpoint)
        context.event_log.log_trace(
            EventType.CONTEXT_COMPACTED,
            context.step,
            checkpoint_id=checkpoint.checkpoint_id,
            source_event_ids=list(source_event_ids),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            repo_revision=checkpoint.repo_revision,
            summary_method=checkpoint.summary_method,
            summary_hash=checkpoint.summary_hash,
            retained_tail=checkpoint.retained_tail,
            keep_recent_tokens=checkpoint.keep_recent_tokens,
            retained_tail_tokens=checkpoint.retained_tail_tokens,
        )
        return None

    def _summary(self, messages: list[LLMMessage], limit: int) -> str:
        lines = [f"- {message.role}: {' '.join(message.content.split())}" for message in messages]
        return "\n".join(lines)[:limit]


def _repo_revision(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
