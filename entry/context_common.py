"""Shared construction of the configured context policy for production entrypoints."""

from __future__ import annotations

import dataclasses

from context.compaction import TraceableCompaction
from llm.router import create_backend_from_config
from config.schema import merge_cli_overrides


def new_worker_backend(config, role):
    """Build an independent client and model-aware budget for a worker role."""
    worker_config = dataclasses.replace(
        config, llm=dataclasses.replace(config.llm), agent=dataclasses.replace(config.agent)
    )
    model = config.agent.worker_models.get(role)
    if model:
        merge_cli_overrides(worker_config, model=model)
    llm = worker_config.llm
    backend = create_backend_from_config({
        "provider": llm.provider, "protocol": llm.protocol, "model": llm.model,
        "api_key": llm.api_key or None, "base_url": llm.base_url or None,
        "max_tokens": llm.max_tokens, "strict_tool_schema": llm.strict_tool_schema,
    })
    return backend, worker_config.agent.budget_tokens


def configured_agent_options(config):
    """入口共享预算与工具配置，保留携带模型 Counter 元数据的预算对象。"""
    return {
        "max_steps": config.agent.max_steps,
        "finish_policy": config.agent.finish_policy,
        "run_profile": config.agent.run_profile,
        "multi_agent_enabled": config.agent.multi_agent_enabled,
        "max_workers": config.agent.max_workers,
        "max_delegate_tasks": config.agent.max_delegate_tasks,
        "total_worker_tokens": config.agent.total_worker_tokens,
        "worker_timeout_seconds": config.agent.worker_timeout_seconds,
        "max_model_calls": config.agent.max_model_calls,
        "max_total_tokens": config.agent.max_total_tokens,
        "budget_tokens": config.agent.budget_tokens,
        "history_max_messages": config.context.history_window * 2,
        "repo_map_mode": config.context.repo_map_mode,
        "repo_map_budget": config.context.repo_map_budget,
        "repo_map_summary_budget": config.context.repo_map_summary_budget,
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
