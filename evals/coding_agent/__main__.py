"""CLI entrypoint for the P2-0 Coding Agent Evaluation Harness."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from agent.core import AgentConfig
from agent.runner import ExecutionRunner
from config.schema import load_config
from entry.cli import _build_registry
from evals.coding_agent.runner import EvaluationHarness, validate_suite_references, write_not_executed
from evals.coding_agent.schema import EvaluationSuite
from llm.router import create_backend_from_config

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SUITE = _ROOT / "evals" / "fixtures" / "coding_agent" / "suite.json"


def _real_runner_factory(cfg, backend, suite: EvaluationSuite):
    def factory(task, repo, trace_dir, trial_id):
        registry = _build_registry(cfg, default_cwd=str(repo), workspace=str(repo))
        config = AgentConfig(
            max_steps=int(suite.defaults.get("max_steps", cfg.agent.max_steps)),
            budget_tokens=int(suite.defaults.get("budget_tokens", cfg.agent.budget_tokens)),
            history_max_messages=cfg.context.history_window * 2,
            stream=False,
            repo_map_mode="incremental",
            repo_map_cache_dir=str(trace_dir.parent / "repo-map-cache"),
        )
        return ExecutionRunner(backend=backend, registry=registry, config=config, log_dir=str(trace_dir))
    return factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=str(_DEFAULT_SUITE))
    parser.add_argument("--variant", default="baseline_react")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-dir", default="evals/results/coding_agent_eval")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--real-model", action="store_true", help="Explicitly execute the configured real model")
    parser.add_argument("--config", default=None)
    parser.add_argument("--reason", default="provider_credentials_not_available_in_execution_environment")
    args = parser.parse_args()

    suite = EvaluationSuite.load(args.suite)
    with tempfile.TemporaryDirectory(prefix="forge-agent-eval-reference-") as temp_dir:
        reference = validate_suite_references(suite, temp_dir)

    if not args.real_model:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason=args.reason,
        )
        print(json.dumps({"reference_validation": reference, "report": report}, ensure_ascii=False, indent=2))
        return 0

    cfg = load_config(args.config)
    if not cfg.llm.api_key:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason="provider_credentials_not_available",
        )
        print(json.dumps({"reference_validation": reference, "report": report}, ensure_ascii=False, indent=2))
        return 0

    try:
        backend = create_backend_from_config({
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "api_key": cfg.llm.api_key or None,
            "base_url": cfg.llm.base_url or None,
            "max_tokens": cfg.llm.max_tokens,
        })
    except ValueError as exc:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason=f"provider_not_configured:{exc}",
        )
        print(json.dumps({"reference_validation": reference, "report": report}, ensure_ascii=False, indent=2))
        return 0

    harness = EvaluationHarness(
        suite=suite,
        output_dir=args.output_dir,
        runner_factory=_real_runner_factory(cfg, backend, suite),
        variant=args.variant,
        repetitions=args.repetitions,
        evidence_kind="real_model",
        real_model_executed=True,
        run_metadata={
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
        },
    )
    results = harness.run(args.task)
    print(json.dumps({"trials": len(results), "output_dir": str(Path(args.output_dir).resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
