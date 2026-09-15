"""Deterministic, traceable history compaction for prepare_next_turn."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from agent.core import PrepareNextTurnContext, PrepareNextTurnResult
from agent.task import EventType
from context.repository_state import repository_fingerprint
from context.token_budget import (
    estimate_messages_tokens,
    history_unit_tokens,
    recent_history_units,
)
from llm.base import LLMMessage


@dataclass(frozen=True)
class CompactionEntry:
    """独立于 canonical history 的压缩状态，只在构建 model-visible view 时渲染。"""

    checkpoint_id: str
    created_at: str
    summary_method: str
    summary_hash: str
    summary_text: str
    source_event_ids: tuple[str, ...]
    previous_checkpoint_id: str | None
    keep_recent_tokens: int
    retained_tail_tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


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
    previous_checkpoint_id: str | None
    projected_input_tokens: int
    projected_after_tokens: int
    available_input_tokens: int
    pressure_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


class TraceableCompaction:
    """Compact old history when the full next request approaches its available budget."""

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
        self.entries: list[CompactionEntry] = []
        self.checkpoints: list[CompactionCheckpoint] = []
        self._lineage_checkpoint_id: str | None = None

    def restore_checkpoint_lineage(self, checkpoint_id: str | None) -> None:
        """恢复持久化 Session 的 lineage cursor，不把旧 summary 恢复成事实。"""
        self.entries.clear()
        self.checkpoints.clear()
        self._lineage_checkpoint_id = checkpoint_id

    def reset_checkpoint_lineage(self) -> None:
        """新上下文从新的 checkpoint 链开始，同时清空待持久化的旧 checkpoint。"""
        self.entries.clear()
        self.checkpoints.clear()
        self._lineage_checkpoint_id = None

    def __call__(self, context: PrepareNextTurnContext):
        messages = context.history.to_list()
        raw = context.history.to_dicts()
        before_tokens = estimate_messages_tokens(raw)
        pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=raw,
            tools=context.tool_schemas,
        )
        if len(messages) <= 1 or pressure.ratio < self.threshold:
            return None

        recent_units = recent_history_units(raw, self.keep_recent_tokens)
        if not recent_units:
            return None
        tail_start = recent_units[0].indices[0]
        dropped = messages[1:tail_start]
        if not dropped:
            return None

        history_budget = context.token_budget.default_plan().history
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
        compacted_raw = [
            {
                "role": message.role,
                "content": message.content,
                **({"event_ref": message.event_ref} if message.event_ref else {}),
            }
            for message in compacted
        ]
        after_tokens = estimate_messages_tokens(compacted_raw)
        after_pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=compacted_raw,
            tools=context.tool_schemas,
        )
        source_event_ids = tuple(
            message.event_ref for message in dropped if message.event_ref is not None
        )
        retained_tail_tokens = sum(
            history_unit_tokens(raw, unit) for unit in recent_units
        )
        checkpoint_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        previous_checkpoint_id = (
            self.entries[-1].checkpoint_id
            if self.entries
            else self._lineage_checkpoint_id
        )
        summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        entry = CompactionEntry(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            summary_method="extractive-v1",
            summary_hash=summary_hash,
            summary_text=summary,
            source_event_ids=source_event_ids,
            previous_checkpoint_id=previous_checkpoint_id,
            keep_recent_tokens=self.keep_recent_tokens,
            retained_tail_tokens=retained_tail_tokens,
        )
        checkpoint = CompactionCheckpoint(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            source_event_ids=source_event_ids,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            repo_revision=repository_fingerprint(context.task.repo_path),
            summary_method=entry.summary_method,
            summary_hash=summary_hash,
            retained_tail=len(messages) - tail_start,
            keep_recent_tokens=self.keep_recent_tokens,
            retained_tail_tokens=retained_tail_tokens,
            previous_checkpoint_id=previous_checkpoint_id,
            projected_input_tokens=pressure.projected_input,
            projected_after_tokens=after_pressure.projected_input,
            available_input_tokens=pressure.available_input,
            pressure_ratio=pressure.ratio,
        )
        self.entries.append(entry)
        self.checkpoints.append(checkpoint)
        self._lineage_checkpoint_id = checkpoint_id
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
            previous_checkpoint_id=checkpoint.previous_checkpoint_id,
            projected_input_tokens=checkpoint.projected_input_tokens,
            projected_after_tokens=checkpoint.projected_after_tokens,
            available_input_tokens=checkpoint.available_input_tokens,
            pressure_ratio=checkpoint.pressure_ratio,
        )
        # 只覆盖下一次模型可见 History；canonical ConversationHistory 保持原样。
        return PrepareNextTurnResult(history_override=tuple(compacted))

    def _summary(self, messages: list[LLMMessage], limit: int) -> str:
        lines = [f"- {message.role}: {' '.join(message.content.split())}" for message in messages]
        return "\n".join(lines)[:limit]
