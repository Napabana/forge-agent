"""Controlled real-provider cache-prefix A/B benchmark for Repo Map prompt layout.

This is intentionally not a Coding Agent success benchmark. It keeps the model,
tool schemas, user message, and dynamic repository payload shape fixed while
changing only whether stable textual tool descriptions appear before or after
the dynamic repository section in the system prompt.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent.prompt import build_system_prompt
from config.schema import load_config
from entry.cli import _build_registry
from llm.base import LLMMessage, LLMToolSchema
from llm.router import create_backend_from_config

VARIANTS = ("legacy_dynamic_first", "stable_prefix")


def render_layout_prompt(*, repo_path: str, tools: list[LLMToolSchema], repo_summary: str, variant: str) -> str:
    """Render semantically identical system content with one ordering change."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown layout variant: {variant}")
    stable = build_system_prompt(repo_path, tools, repo_summary=repo_summary)
    if variant == "stable_prefix":
        return stable
    tools_marker = "## Available tools\n"
    repo_marker = "\n## Repository\n"
    if tools_marker not in stable or repo_marker not in stable:
        raise ValueError("system prompt markers changed; update cache A/B renderer")
    prefix, rest = stable.split(tools_marker, 1)
    tool_block, repo_block = rest.split(repo_marker, 1)
    return prefix + "## Repository\n" + repo_block.rstrip() + "\n\n## Available tools\n" + tool_block.strip() + "\n"


def dynamic_repo_summary(namespace: str, call_index: int, *, lines: int = 96) -> str:
    """Deterministic, equal-shape dynamic payload whose early content changes per call."""
    return "\n".join([
        f"DYNAMIC_REPO_CONTEXT namespace={namespace} revision={call_index}",
        *[f"src/module_{i:03d}.py: class Service{i}; def handle_{i}(request): revision_{call_index}_{i}" for i in range(lines)],
    ])


def _backend_from_cfg(cfg: Any, max_tokens: int):
    return create_backend_from_config({
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "api_key": cfg.llm.api_key or None,
        "base_url": cfg.llm.base_url or None,
        "max_tokens": max_tokens,
    })


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for variant in VARIANTS:
        measured = [row for row in rows if row["variant"] == variant and not row["warmup"]]
        if not measured:
            continue
        total_input = sum(int(row["input_tokens"]) for row in measured)
        total_cached = sum(int(row["cached_input_tokens"]) for row in measured)
        uncached = [int(row["input_tokens"]) - int(row["cached_input_tokens"]) for row in measured]
        result[variant] = {
            "measured_calls": len(measured),
            "mean_input_tokens": statistics.mean(int(row["input_tokens"]) for row in measured),
            "mean_cached_input_tokens": statistics.mean(int(row["cached_input_tokens"]) for row in measured),
            "mean_uncached_input_tokens": statistics.mean(uncached),
            "aggregate_cache_fraction": (total_cached / total_input) if total_input else 0.0,
            "mean_latency_seconds": statistics.mean(float(row["latency_seconds"]) for row in measured),
            "provider_usage_estimated_calls": sum(bool(row["estimated"]) for row in measured),
        }
    return {"variants": result, "measured_run_count": sum(v["measured_calls"] for v in result.values())}


def run_benchmark(*, config_path: str | Path | None, output: str | Path, repetitions: int = 1, measured_calls: int = 4, max_tokens: int = 64, namespace: str | None = None) -> dict[str, Any]:
    if repetitions < 1 or measured_calls < 1:
        raise ValueError("repetitions and measured_calls must be >= 1")
    cfg = load_config(config_path)
    backend = _backend_from_cfg(cfg, max_tokens)
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ns = namespace or f"forge-cache-{uuid.uuid4().hex[:12]}"
    rows: list[dict[str, Any]] = []

    with TemporaryDirectory(prefix="forge-cache-ab-") as tmp:
        workspace = Path(tmp)
        registry = _build_registry(cfg, worktree_path=str(workspace), workspace=str(workspace))
        tools = registry.get_schemas()
        user_message = LLMMessage(role="user", content="Inspect the repository context and briefly identify the most relevant module.")
        with (out / "raw.jsonl").open("w", encoding="utf-8") as stream:
            for repeat in range(1, repetitions + 1):
                order = VARIANTS if repeat % 2 else tuple(reversed(VARIANTS))
                for variant in order:
                    for call_index in range(0, measured_calls + 1):
                        summary = dynamic_repo_summary(f"{ns}-r{repeat}", call_index)
                        prompt = render_layout_prompt(repo_path=str(workspace), tools=tools, repo_summary=summary, variant=variant)
                        started = time.perf_counter()
                        response = backend.complete([LLMMessage(role="system", content=prompt), user_message], tools)
                        elapsed = time.perf_counter() - started
                        usage = response.usage
                        row = {
                            "variant": variant,
                            "repeat": repeat,
                            "call_index": call_index,
                            "warmup": call_index == 0,
                            "input_tokens": usage.input_tokens,
                            "cached_input_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "uncached_input_tokens": max(0, usage.input_tokens - usage.cached_tokens),
                            "output_tokens": usage.output_tokens,
                            "estimated": usage.estimated,
                            "latency_seconds": elapsed,
                        }
                        rows.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stream.flush()

    report = _aggregate(rows)
    metadata = {
        "schema_version": 1,
        "evidence_level": "Real-provider Controlled Microbenchmark",
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "repetitions": repetitions,
        "measured_calls_per_variant_per_repeat": measured_calls,
        "warmup_calls_per_variant_per_repeat": 1,
        "max_output_tokens": max_tokens,
        "namespace": ns,
        "variants": list(VARIANTS),
        "claim_boundary": "This isolates prompt ordering and provider-reported cache usage. It is not a Coding Agent success benchmark or an end-to-end latency benchmark.",
    }
    (out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"metadata": metadata, "report": report, "rows": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--output", default="../forge-agent-evals/repo-map-prompt-cache-ab")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--measured-calls", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--namespace")
    args = parser.parse_args()
    result = run_benchmark(config_path=args.config, output=args.output, repetitions=args.repetitions, measured_calls=args.measured_calls, max_tokens=args.max_tokens, namespace=args.namespace)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
