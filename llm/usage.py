"""Provider-neutral token usage values and session aggregation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """One LLM call using provider-style inclusive token totals.

    Cache read/write counts are subsets of ``input_tokens``. Reasoning tokens
    are a subset of ``output_tokens``. Subsets are never added to the total.
    """

    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_tokens": self.total_tokens}


@dataclass
class SessionUsage:
    """Aggregate usage for a run or persistent chat session."""

    llm_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_calls: int = 0
    unattributed_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.unattributed_tokens

    def record(self, usage: TokenUsage) -> None:
        self.llm_calls += 1
        self.input_tokens += usage.input_tokens
        self.cached_tokens += usage.cached_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.estimated_calls += int(usage.estimated)

    def add(self, usage: "SessionUsage") -> None:
        self.llm_calls += usage.llm_calls
        self.input_tokens += usage.input_tokens
        self.cached_tokens += usage.cached_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.estimated_calls += usage.estimated_calls
        self.unattributed_tokens += usage.unattributed_tokens

    def snapshot(self) -> "SessionUsage":
        return SessionUsage.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, int]:
        return {**asdict(self), "total_tokens": self.total_tokens}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionUsage":
        if not data:
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{key: int(value) for key, value in data.items() if key in fields})
