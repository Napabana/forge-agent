"""Model capability metadata shared by LLM routing and context budgeting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    """Reliable model-level limits known to Forge.

    ``None`` means the provider/proxy configuration did not provide a value Forge can
    safely treat as authoritative. ``source`` is diagnostic metadata only.
    """

    context_window: int | None = None
    max_output_tokens: int | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
