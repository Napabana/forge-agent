"""Shared construction of the configured context policy for production entrypoints."""

from __future__ import annotations

from context.compaction import TraceableCompaction


def configured_agent_options(config):
    """入口共享预算与工具配置，保留携带模型 Counter 元数据的预算对象。"""
    return {
        "max_steps": config.agent.max_steps,
        "max_model_calls": config.agent.max_model_calls,
        "max_total_tokens": config.agent.max_total_tokens,
        "budget_tokens": config.agent.budget_tokens,
        "history_max_messages": config.context.history_window * 2,
        "repo_map_mode": config.context.repo_map_mode,
        "repo_map_budget": config.context.repo_map_budget,
        "test_python_executable": config.tools.test.python_executable,
    }


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
