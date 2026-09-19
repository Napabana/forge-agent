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
_DEFAULT_SKILLS = _ROOT / "evals" / "fixtures" / "coding_agent" / "skills"


def _planning_mode_for_variant(variant: str) -> str:
    mapping = {
        "baseline_react": "off",
        "planning": "always",
        "planning_recovery": "always",
        "planning_recovery_skills": "always",
    }
    try:
        return mapping[variant]
    except KeyError as exc:
        raise ValueError(
            f"unsupported coding-agent architecture variant {variant!r}; "
            f"expected one of: {', '.join(mapping)}"
        ) from exc


def _recovery_mode_for_variant(variant: str) -> str:
    mapping = {
        "baseline_react": "off",
        "planning": "off",
        "planning_recovery": "structured",
        "planning_recovery_skills": "structured",
    }
    try:
        return mapping[variant]
    except KeyError as exc:
        raise ValueError(
            f"unsupported coding-agent architecture variant {variant!r}; "
            f"expected one of: {', '.join(mapping)}"
        ) from exc


def _skills_enabled_for_variant(variant: str) -> bool:
    mapping = {
        "baseline_react": False,
        "planning": False,
        "planning_recovery": False,
        "planning_recovery_skills": True,
    }
    try:
        return mapping[variant]
    except KeyError as exc:
        raise ValueError(
            f"unsupported coding-agent architecture variant {variant!r}; "
            f"expected one of: {', '.join(mapping)}"
        ) from exc


def _real_runner_factory(
    cfg,
    backend,
    suite: EvaluationSuite,
    planning_mode: str,
    recovery_mode: str,
    skills_enabled: bool,
    skills_global_dir: str | None,
):
    def factory(task, repo, trace_dir, trial_id):
        registry = _build_registry(cfg, default_cwd=str(repo), workspace=str(repo))
        config = AgentConfig(
            max_steps=int(suite.defaults.get("max_steps", cfg.agent.max_steps)),
            budget_tokens=int(suite.defaults.get("budget_tokens", cfg.agent.budget_tokens)),
            history_max_messages=cfg.context.history_window * 2,
            planning_mode=planning_mode,
            recovery_mode=recovery_mode,
            recovery_max_attempts=cfg.agent.recovery_max_attempts,
            skills_enabled=skills_enabled,
            skills_global_dir=skills_global_dir,
            skills_max_loaded=cfg.agent.skills_max_loaded,
            skills_max_chars=cfg.agent.skills_max_chars,
            skills_reference_max_chars=cfg.agent.skills_reference_max_chars,
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
    planning_mode = _planning_mode_for_variant(args.variant)
    recovery_mode = _recovery_mode_for_variant(args.variant)
    skills_enabled = _skills_enabled_for_variant(args.variant)
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
        runner_factory=_real_runner_factory(
            cfg,
            backend,
            suite,
            planning_mode,
            recovery_mode,
            skills_enabled,
            str(_DEFAULT_SKILLS) if skills_enabled else cfg.agent.skills_global_dir,
        ),
        variant=args.variant,
        repetitions=args.repetitions,
        evidence_kind="real_model",
        real_model_executed=True,
        run_metadata={
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "planning_mode": planning_mode,
            "recovery_mode": recovery_mode,
            "recovery_max_attempts": cfg.agent.recovery_max_attempts,
            "skills_enabled": skills_enabled,
            "skills_global_dir": (
                str(_DEFAULT_SKILLS) if skills_enabled else cfg.agent.skills_global_dir
            ),
        },
    )
    results = harness.run(args.task)
    print(json.dumps({"trials": len(results), "output_dir": str(Path(args.output_dir).resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
