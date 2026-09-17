"""Traceable context policy: pruning first, hybrid structured compaction second."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from agent.core import PrepareNextTurnContext, PrepareNextTurnResult
from agent.task import EventType
from context.repository_state import repository_fingerprint
from context.structured_compaction import (
    LLMSemanticSummarizer,
    SemanticSummarizer,
    build_deterministic_evidence,
    build_structured_state,
    fallback_user_excerpts,
    render_structured_context,
)
from context.token_budget import history_unit_tokens, recent_history_units
from context.tool_pruning import DeterministicToolPruner, PruningResult
from llm.base import LLMBackend, LLMMessage, MockBackend
from llm.usage import SessionUsage, TokenUsage


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
class ActiveCompactedView:
    """同一 Task 内可复用的 summary + canonical delta 视图，不作为事实源持久化。"""

    summary_text: str
    source_end_index: int
    source_prefix_hash: str
    checkpoint_id: str
    task_description: str


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
    pruning_method: str | None
    pruned_event_ids: tuple[str, ...]
    pruned_before_tokens: int
    pruned_after_tokens: int
    pruned_units: int
    summary_usage: dict
    semantic_packet_truncated: bool
    semantic_error: str | None
    semantic_duration_ms: float | None

    def to_dict(self) -> dict:
        return asdict(self)


class TraceableCompaction:
    """先做 deterministic pruning；仍高压时做 Hybrid structured compaction。"""

    def __init__(
        self,
        *,
        threshold: float = 0.8,
        target_ratio: float = 0.5,
        keep_recent_tokens: int = 8_000,
        max_summary_chars: int = 2_000,
        pruner: DeterministicToolPruner | None = None,
        semantic_summarizer: SemanticSummarizer | None = None,
    ) -> None:
        if not 0 < target_ratio < threshold <= 1:
            raise ValueError("require 0 < target_ratio < threshold <= 1")
        if keep_recent_tokens <= 0:
            raise ValueError("keep_recent_tokens must be positive")
        self.threshold = threshold
        self.target_ratio = target_ratio
        self.keep_recent_tokens = keep_recent_tokens
        self.max_summary_chars = max_summary_chars
        self.pruner = pruner or DeterministicToolPruner()
        self.semantic_summarizer = semantic_summarizer
        self.entries: list[CompactionEntry] = []
        self.checkpoints: list[CompactionCheckpoint] = []
        self._lineage_checkpoint_id: str | None = None
        self._active_view: ActiveCompactedView | None = None
        self._pending_usage = SessionUsage()

    def bind_backend(self, backend: LLMBackend) -> None:
        """真实 Chat backend 自动绑定 semantic summarizer；脚本 Mock 必须显式注入。"""
        if self.semantic_summarizer is None and not isinstance(backend, MockBackend):
            self.semantic_summarizer = LLMSemanticSummarizer(backend)

    def consume_usage(self) -> SessionUsage:
        """取走本轮 semantic side-call usage，供 Chat 合并到最终 RunResult。"""
        snapshot = self._pending_usage.snapshot()
        self._pending_usage = SessionUsage()
        return snapshot

    def restore_checkpoint_lineage(self, checkpoint_id: str | None) -> None:
        """恢复 lineage cursor；V1 不恢复旧 summary，首次高压时重新 compact。"""
        self.entries.clear()
        self.checkpoints.clear()
        self._lineage_checkpoint_id = checkpoint_id
        self._active_view = None
        self._pending_usage = SessionUsage()

    def reset_checkpoint_lineage(self) -> None:
        """新上下文从新的 checkpoint 链开始，并清掉进程内 active summary。"""
        self.entries.clear()
        self.checkpoints.clear()
        self._lineage_checkpoint_id = None
        self._active_view = None
        self._pending_usage = SessionUsage()

    def __call__(self, context: PrepareNextTurnContext):
        messages = context.history.to_list()
        if len(messages) <= 1:
            return None

        active = self._reuse_active_view(context, messages)
        if active is not None:
            return active

        raw = context.history.to_dicts()
        before_tokens = context.token_budget.count_messages(raw)
        pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=raw,
            tools=context.tool_schemas,
        )
        if pressure.ratio < self.threshold:
            return None

        # recent raw tail 只按 canonical history 计算一次，并复用本轮 TokenCounter。
        recent_units = recent_history_units(
            raw,
            self.keep_recent_tokens,
            counter=context.token_budget.counter,
        )
        if not recent_units:
            return None
        tail_start = recent_units[0].indices[0]
        retained_tail_tokens = sum(
            history_unit_tokens(raw, unit, counter=context.token_budget.counter)
            for unit in recent_units
        )

        pruning = self.pruner.prune(messages, protected_from_index=tail_start)
        pruned_messages = list(pruning.messages)
        pruned_raw = _message_dicts(pruned_messages)
        pruned_pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=pruned_raw,
            tools=context.tool_schemas,
        )

        # Stage A 已经把完整 request 拉回 threshold 以下时，不制造 semantic summary。
        if pruning.pruned_units and pruned_pressure.ratio < self.threshold:
            checkpoint = self._make_checkpoint(
                context=context,
                source_event_ids=pruning.pruned_event_ids,
                before_tokens=before_tokens,
                after_tokens=context.token_budget.count_messages(pruned_raw),
                summary_method="none",
                summary_hash="",
                retained_tail=len(messages) - tail_start,
                retained_tail_tokens=retained_tail_tokens,
                pressure=pressure,
                projected_after_tokens=pruned_pressure.projected_input,
                pruning=pruning,
            )
            self._record_checkpoint(context, checkpoint)
            return PrepareNextTurnResult(history_override=tuple(pruned_messages))

        dropped = pruned_messages[1:tail_start]
        if not dropped:
            if pruning.pruned_units:
                checkpoint = self._make_checkpoint(
                    context=context,
                    source_event_ids=pruning.pruned_event_ids,
                    before_tokens=before_tokens,
                    after_tokens=context.token_budget.count_messages(pruned_raw),
                    summary_method="none",
                    summary_hash="",
                    retained_tail=len(messages) - tail_start,
                    retained_tail_tokens=retained_tail_tokens,
                    pressure=pressure,
                    projected_after_tokens=pruned_pressure.projected_input,
                    pruning=pruning,
                )
                self._record_checkpoint(context, checkpoint)
                return PrepareNextTurnResult(history_override=tuple(pruned_messages))
            return None

        evidence = build_deterministic_evidence(messages, old_end_index=tail_start)
        semantic_fields = None
        semantic_usage = TokenUsage()
        semantic_error: str | None = None
        semantic_duration_ms: float | None = None
        packet_truncated = False
        semantic_called = self.semantic_summarizer is not None

        if semantic_called:
            started_at = time.perf_counter()
            context.event_log.log_trace(
                EventType.CONTEXT_COMPACTION_STARTED,
                context.step,
                mode="semantic",
                pressure_ratio=pruned_pressure.ratio,
                projected_input_tokens=pruned_pressure.projected_input,
            )
            kwargs = dict(
                task_description=context.task.description,
                old_messages=dropped,
                recent_messages=messages[tail_start:],
                evidence=evidence,
            )
            if isinstance(self.semantic_summarizer, LLMSemanticSummarizer):
                result = self.semantic_summarizer.summarize(
                    **kwargs,
                    token_budget=context.token_budget,
                )
            else:
                result = self.semantic_summarizer.summarize(**kwargs)
            semantic_duration_ms = (time.perf_counter() - started_at) * 1000
            semantic_fields = result.fields
            semantic_usage = result.usage
            semantic_error = result.error
            packet_truncated = result.packet_truncated
            self._pending_usage.record(semantic_usage)
            if semantic_fields is None:
                context.event_log.log_trace(
                    EventType.CONTEXT_COMPACTION_FAILED,
                    context.step,
                    mode="semantic",
                    duration_ms=semantic_duration_ms,
                    error=semantic_error or "semantic summary returned no fields",
                    packet_truncated=packet_truncated,
                    summary_usage=semantic_usage.to_dict(),
                )
                if self._active_view is not None:
                    active_messages = self._active_messages(messages)
                    if active_messages is not None:
                        return PrepareNextTurnResult(history_override=tuple(active_messages))

        if not semantic_called:
            history_budget = context.token_budget.default_plan().history
            summary_method = "extractive-v1"
            summary = _extractive_summary(
                dropped,
                min(self.max_summary_chars, int(history_budget * self.target_ratio * 4)),
            )
        else:
            fallback_excerpts = ()
            summary_method = "structured-hybrid-v1"
            if semantic_fields is None:
                summary_method = "structured-fallback-v1"
                fallback_excerpts = fallback_user_excerpts(dropped)

            state = build_structured_state(
                goal=context.task.description,
                semantic=semantic_fields,
                evidence=evidence,
                fallback_excerpts=fallback_excerpts,
            )
            summary = render_structured_context(
                state,
                max_chars=max(900, self.max_summary_chars),
            )

        compacted = [
            pruned_messages[0],
            _summary_message(summary),
            *pruned_messages[tail_start:],
        ]
        compacted_raw = _message_dicts(compacted)
        after_tokens = context.token_budget.count_messages(compacted_raw)
        after_pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=compacted_raw,
            tools=context.tool_schemas,
        )
        source_event_ids = tuple(
            message.event_ref
            for message in messages[1:tail_start]
            if message.event_ref is not None
        )
        checkpoint_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        previous_checkpoint_id = self._lineage_checkpoint_id
        summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        entry = CompactionEntry(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            summary_method=summary_method,
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
            pruning_method=(self.pruner.method if pruning.pruned_units else None),
            pruned_event_ids=pruning.pruned_event_ids,
            pruned_before_tokens=pruning.before_tokens,
            pruned_after_tokens=pruning.after_tokens,
            pruned_units=pruning.pruned_units,
            summary_usage=semantic_usage.to_dict() if semantic_called else {},
            semantic_packet_truncated=packet_truncated,
            semantic_error=semantic_error,
            semantic_duration_ms=semantic_duration_ms,
        )
        self.entries.append(entry)
        self._record_checkpoint(context, checkpoint)
        self._active_view = ActiveCompactedView(
            summary_text=summary,
            source_end_index=tail_start,
            source_prefix_hash=_prefix_hash(messages, tail_start),
            checkpoint_id=checkpoint_id,
            task_description=context.task.description,
        )
        return PrepareNextTurnResult(history_override=tuple(compacted))

    def _reuse_active_view(
        self,
        context: PrepareNextTurnContext,
        messages: list[LLMMessage],
    ) -> PrepareNextTurnResult | None:
        """仅在同一 Task 内复用 active summary；新 Chat round 的 Goal 变化时立即失效。"""
        active = self._active_view
        if active is not None and active.task_description != context.task.description:
            self._active_view = None
            return None

        active_messages = self._active_messages(messages)
        if active_messages is None:
            return None
        active_raw = _message_dicts(active_messages)
        active_pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=active_raw,
            tools=context.tool_schemas,
        )
        if active_pressure.ratio < self.threshold:
            return PrepareNextTurnResult(history_override=tuple(active_messages))

        recent_units = recent_history_units(
            active_raw,
            self.keep_recent_tokens,
            counter=context.token_budget.counter,
        )
        if not recent_units:
            return None
        protected_from = recent_units[0].indices[0]
        pruning = self.pruner.prune(active_messages, protected_from_index=protected_from)
        if not pruning.pruned_units:
            return None
        pruned_messages = list(pruning.messages)
        pruned_pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=_message_dicts(pruned_messages),
            tools=context.tool_schemas,
        )
        if pruned_pressure.ratio < self.threshold:
            return PrepareNextTurnResult(history_override=tuple(pruned_messages))
        return None

    def _active_messages(self, messages: list[LLMMessage]) -> list[LLMMessage] | None:
        active = self._active_view
        if active is None:
            return None
        if active.source_end_index > len(messages):
            self._active_view = None
            return None
        if _prefix_hash(messages, active.source_end_index) != active.source_prefix_hash:
            self._active_view = None
            return None
        return [
            messages[0],
            _summary_message(active.summary_text),
            *messages[active.source_end_index:],
        ]

    def _make_checkpoint(
        self,
        *,
        context: PrepareNextTurnContext,
        source_event_ids: tuple[str, ...],
        before_tokens: int,
        after_tokens: int,
        summary_method: str,
        summary_hash: str,
        retained_tail: int,
        retained_tail_tokens: int,
        pressure,
        projected_after_tokens: int,
        pruning: PruningResult,
    ) -> CompactionCheckpoint:
        return CompactionCheckpoint(
            checkpoint_id=uuid.uuid4().hex[:12],
            created_at=datetime.now(timezone.utc).isoformat(),
            source_event_ids=source_event_ids,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            repo_revision=repository_fingerprint(context.task.repo_path),
            summary_method=summary_method,
            summary_hash=summary_hash,
            retained_tail=retained_tail,
            keep_recent_tokens=self.keep_recent_tokens,
            retained_tail_tokens=retained_tail_tokens,
            previous_checkpoint_id=self._lineage_checkpoint_id,
            projected_input_tokens=pressure.projected_input,
            projected_after_tokens=projected_after_tokens,
            available_input_tokens=pressure.available_input,
            pressure_ratio=pressure.ratio,
            pruning_method=self.pruner.method,
            pruned_event_ids=pruning.pruned_event_ids,
            pruned_before_tokens=pruning.before_tokens,
            pruned_after_tokens=pruning.after_tokens,
            pruned_units=pruning.pruned_units,
            summary_usage={},
            semantic_packet_truncated=False,
            semantic_error=None,
            semantic_duration_ms=None,
        )

    def _record_checkpoint(
        self,
        context: PrepareNextTurnContext,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        self.checkpoints.append(checkpoint)
        self._lineage_checkpoint_id = checkpoint.checkpoint_id
        context.event_log.log_trace(
            EventType.CONTEXT_COMPACTED,
            context.step,
            checkpoint_id=checkpoint.checkpoint_id,
            source_event_ids=list(checkpoint.source_event_ids),
            before_tokens=checkpoint.before_tokens,
            after_tokens=checkpoint.after_tokens,
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
            pruning_method=checkpoint.pruning_method,
            pruned_event_ids=list(checkpoint.pruned_event_ids),
            pruned_before_tokens=checkpoint.pruned_before_tokens,
            pruned_after_tokens=checkpoint.pruned_after_tokens,
            pruned_units=checkpoint.pruned_units,
            summary_usage=checkpoint.summary_usage,
            semantic_packet_truncated=checkpoint.semantic_packet_truncated,
            semantic_error=checkpoint.semantic_error,
            semantic_duration_ms=checkpoint.semantic_duration_ms,
        )


def _extractive_summary(messages: list[LLMMessage], limit: int) -> str:
    """C4 兼容摘要：仅在没有 semantic summarizer 的程序化路径使用。"""
    lines = [f"- {message.role}: {' '.join(message.content.split())}" for message in messages]
    return "\n".join(lines)[:limit]


def _summary_message(summary: str) -> LLMMessage:
    return LLMMessage(
        role="user",
        content="[Compacted earlier context; full tool results remain in EventLog]\n" + summary,
    )


def _prefix_hash(messages: list[LLMMessage], end_index: int) -> str:
    digest = hashlib.sha256()
    for message in messages[:end_index]:
        digest.update(message.role.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.content.encode("utf-8"))
        digest.update(b"\0")
        digest.update((message.event_ref or "").encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _message_dicts(messages: list[LLMMessage]) -> list[dict]:
    return [
        {
            "role": message.role,
            "content": message.content,
            **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
            **({"event_ref": message.event_ref} if message.event_ref else {}),
        }
        for message in messages
    ]
