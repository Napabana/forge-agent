from __future__ import annotations

from llm.base import LLMToolSchema
from evals.repo_map_prompt_cache_ablation import _aggregate, render_layout_prompt


def test_prompt_cache_layout_variants_only_reorder_repo_and_tools():
    tools = [LLMToolSchema(name="file_read", description="read", parameters={})]
    stable = render_layout_prompt(repo_path="/repo", tools=tools, repo_summary="DYNAMIC", variant="stable_prefix")
    legacy = render_layout_prompt(repo_path="/repo", tools=tools, repo_summary="DYNAMIC", variant="legacy_dynamic_first")
    assert stable.index("## Available tools") < stable.index("DYNAMIC")
    assert legacy.index("DYNAMIC") < legacy.index("## Available tools")
    for text in ("file_read", "DYNAMIC", "Path: /repo"):
        assert stable.count(text) == legacy.count(text) == 1


def test_prompt_cache_aggregate_excludes_warmup_and_reports_fraction():
    rows = [
        {"variant": "stable_prefix", "warmup": True, "input_tokens": 100, "cached_input_tokens": 0, "latency_seconds": 1.0, "estimated": False},
        {"variant": "stable_prefix", "warmup": False, "input_tokens": 100, "cached_input_tokens": 60, "latency_seconds": 1.0, "estimated": False},
        {"variant": "legacy_dynamic_first", "warmup": True, "input_tokens": 100, "cached_input_tokens": 0, "latency_seconds": 1.0, "estimated": False},
        {"variant": "legacy_dynamic_first", "warmup": False, "input_tokens": 100, "cached_input_tokens": 20, "latency_seconds": 1.0, "estimated": False},
    ]
    report = _aggregate(rows)
    assert report["variants"]["stable_prefix"]["aggregate_cache_fraction"] == 0.6
    assert report["variants"]["stable_prefix"]["mean_uncached_input_tokens"] == 40
    assert report["variants"]["legacy_dynamic_first"]["aggregate_cache_fraction"] == 0.2
