"""Shared construction of the configured context policy for production entrypoints."""

from __future__ import annotations

from context.compaction import TraceableCompaction


def build_context_policy(config, backend):
    """Create an isolated context policy for one run/session.

    The policy owns checkpoint lineage, active compacted views and side-call usage,
    so it must not be shared between independent runs.
    """
    context = config.context
    if context.compaction_mode == "off":
        return None
    if context.compaction_mode != "traceable":
        raise ValueError(f"Unsupported context.compaction_mode: {context.compaction_mode!r}")

    policy = TraceableCompaction(
        threshold=context.compaction_threshold,
        target_ratio=context.compaction_target_ratio,
        keep_recent_tokens=context.compaction_keep_recent_tokens,
    )
    policy.bind_backend(backend)
    return policy
