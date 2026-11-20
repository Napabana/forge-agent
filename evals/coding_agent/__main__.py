"""Run the coding agent evaluation suite."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from agent.core import AgentConfig
from agent.runner import ExecutionRunner
from config.schema import MCPConfig, MCPServerConfig, load_config
from entry.cli import _build_run_registry
from entry.context_common import build_context_policy, configured_agent_options, new_worker_backend
from context.token_budget import token_counter_for_model
from evals.coding_agent.runner import EvaluationHarness, validate_suite_references, write_not_executed
from evals.coding_agent.schema import EvaluationSuite
from llm.router import create_backend_from_config

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SUITE = _ROOT / "evals" / "fixtures" / "coding_agent" / "suite.json"
_DEFAULT_SKILLS = _ROOT / "evals" / "fixtures" / "coding_agent" / "skills"
_DEFAULT_MCP_SERVER = _ROOT / "evals" / "fixtures" / "coding_agent" / "mcp" / "server.py"


def _planning_mode_for_variant(variant: str) -> str:
    mapping = {
        "baseline_react": "off",
        "supervisor_workers": "off",
        "planning": "always",
        "planning_recovery": "always",
        "planning_recovery_skills": "always",
        "planning_recovery_skills_mcp": "always",
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
        "supervisor_workers": "off",
        "planning": "off",
        "planning_recovery": "structured",
        "planning_recovery_skills": "structured",
        "planning_recovery_skills_mcp": "structured",
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
        "supervisor_workers": False,
        "planning": False,
        "planning_recovery": False,
        "planning_recovery_skills": True,
        "planning_recovery_skills_mcp": True,
    }
    try:
        return mapping[variant]
    except KeyError as exc:
        raise ValueError(
            f"unsupported coding-agent architecture variant {variant!r}; "
            f"expected one of: {', '.join(mapping)}"
        ) from exc


def _mcp_config_for_variant(variant: str) -> MCPConfig:
    supported = {
        "baseline_react",
        "supervisor_workers",
        "planning",
        "planning_recovery",
        "planning_recovery_skills",
        "planning_recovery_skills_mcp",
    }
    if variant not in supported:
        raise ValueError(
            f"unsupported coding-agent architecture variant {variant!r}; "
            f"expected one of: {', '.join(sorted(supported))}"
        )
    if variant != "planning_recovery_skills_mcp":
        return MCPConfig()
    return MCPConfig(
        enabled=True,
        servers=(
            MCPServerConfig(
                id="eval_docs",
                transport="stdio",
                command=sys.executable,
                args=(str(_DEFAULT_MCP_SERVER),),
                timeout_seconds=5.0,
                trust_read_only_annotations=True,
            ),
        ),
    )


def _real_runner_factory(
    cfg,
    backend,
    suite: EvaluationSuite,
    planning_mode: str,
    recovery_mode: str,
    skills_enabled: bool,
    skills_global_dir: str | None,
    mcp_config: MCPConfig,
    variant: str = "baseline_react",
    total_token_budget: int | None = None,
):
    def factory(task, repo, trace_dir, trial_id):
        registry = _build_run_registry(cfg, default_cwd=str(repo), workspace=str(repo))
        options = configured_agent_options(cfg)
        if total_token_budget is not None:
            worker_tokens = total_token_budget // 3 if variant == "supervisor_workers" else 0
            options["max_total_tokens"] = total_token_budget - worker_tokens
            options["multi_agent_enabled"] = variant == "supervisor_workers"
            options["total_worker_tokens"] = worker_tokens or options["total_worker_tokens"]
        # 不把 ContextBudgetSpec 转成 int：它携带模型计数器和请求预留配置。
        options["max_steps"] = int(suite.defaults.get("max_steps", options["max_steps"]))
        if "budget_tokens" in suite.defaults:
            from config.schema import ContextBudgetSpec
            previous = cfg.agent.budget_tokens
            cap = int(suite.defaults["budget_tokens"])
            if cap <= 0:
                raise ValueError("suite budget_tokens must be positive")
            options["budget_tokens"] = ContextBudgetSpec(
                cap, model_context_window=previous.model_context_window,
                model_max_output_tokens=previous.model_max_output_tokens,
                request_max_output_tokens=previous.request_max_output_tokens,
                context_budget_cap=min(cap, previous.context_budget_cap or cap),
                safety_margin_tokens=previous.safety_margin_tokens,
                model_name=previous.model_name, capability_fallback=previous.capability_fallback,
            ) if isinstance(previous, ContextBudgetSpec) else cap
        config = AgentConfig(
            **options,
            planning_mode=planning_mode,
            recovery_mode=recovery_mode,
            recovery_max_attempts=cfg.agent.recovery_max_attempts,
            skills_enabled=skills_enabled,
            skills_global_dir=skills_global_dir,
            skills_max_loaded=cfg.agent.skills_max_loaded,
            skills_max_chars=cfg.agent.skills_max_chars,
            skills_reference_max_chars=cfg.agent.skills_reference_max_chars,
            stream=False,
            prepare_next_turn=build_context_policy(cfg, backend),
            repo_map_cache_dir=str(trace_dir.parent / "repo-map-cache"),
        )
        return ExecutionRunner(
            backend=backend,
            registry=registry,
            config=config,
            log_dir=str(trace_dir),
            mcp_config=mcp_config,
            worker_backend_factory=(lambda role: new_worker_backend(cfg, role)) if variant == "supervisor_workers" else None,
        )
    return factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=str(_DEFAULT_SUITE))
    parser.add_argument("--variant", default="baseline_react")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-dir", default="evals/results/coding_agent_eval")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument(
        "--source-repo",
        default=None,
        help="Local clone used as the frozen source snapshot for real-repository suites",
    )
    parser.add_argument("--real-model", action="store_true", help="Explicitly execute the configured real model")
    parser.add_argument("--config", default=None)
    parser.add_argument("--total-token-budget", type=int, default=None,
                        help="Shared A/B cap; supervisor reserves one third for workers")
    parser.add_argument("--reason", default="provider_credentials_not_available_in_execution_environment")
    args = parser.parse_args()
    if args.total_token_budget is not None and args.total_token_budget < 3:
        parser.error("--total-token-budget must be at least 3")
    if args.variant == "supervisor_workers" and args.total_token_budget is None:
        parser.error("supervisor_workers requires --total-token-budget")

    suite_path = Path(args.suite).resolve()
    suite = EvaluationSuite.load(suite_path)
    source_repo_path = Path(args.source_repo).resolve() if args.source_repo else None
    suite_canonical = json.dumps(
        json.loads(suite_path.read_text(encoding="utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suite_sha256 = hashlib.sha256(suite_canonical).hexdigest()
    planning_mode = _planning_mode_for_variant(args.variant)
    recovery_mode = _recovery_mode_for_variant(args.variant)
    skills_enabled = _skills_enabled_for_variant(args.variant)
    mcp_config = _mcp_config_for_variant(args.variant)
    cfg = load_config(args.config)
    run_metadata = {
        "suite_path": str(suite_path),
        "suite_sha256": suite_sha256,
        "source_repository": suite.source.repository if suite.source else None,
        "source_commit": suite.source.commit if suite.source else None,
        "source_repo_path": str(source_repo_path) if source_repo_path else None,
        "config_source": str(Path(args.config).resolve()) if args.config else "config/default.yaml",
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "max_steps": int(suite.defaults.get("max_steps", cfg.agent.max_steps)),
        "budget_tokens": int(suite.defaults.get("budget_tokens", cfg.agent.budget_tokens)),
        "repo_map_mode": cfg.context.repo_map_mode,
        "repo_map_budget": cfg.context.repo_map_budget,
        "compaction_mode": cfg.context.compaction_mode,
        "compaction_threshold": cfg.context.compaction_threshold,
        "compaction_target_ratio": cfg.context.compaction_target_ratio,
        "compaction_keep_recent_tokens": cfg.context.compaction_keep_recent_tokens,
        "token_counter": token_counter_for_model(cfg.llm.model).name,
        "test_python_executable": cfg.tools.test.python_executable or sys.executable,
        "max_model_calls": cfg.agent.max_model_calls,
        "max_total_tokens": args.total_token_budget or cfg.agent.max_total_tokens,
        "worker_token_reservation": args.total_token_budget // 3 if args.variant == "supervisor_workers" else 0,
        "context_budget_cap": (
            min(int(suite.defaults["budget_tokens"]), cfg.agent.context_budget_cap or int(suite.defaults["budget_tokens"]))
            if "budget_tokens" in suite.defaults else cfg.agent.context_budget_cap
        ),
        "context_safety_margin_tokens": cfg.agent.context_safety_margin_tokens,
        "tool_manifest": _build_run_registry(cfg).tool_names + (["delegate_tasks"] if args.variant == "supervisor_workers" else []),
        "permission_profile": "safe_default_no_confirmation_callback",
        "planning_mode": planning_mode,
        "recovery_mode": recovery_mode,
        "recovery_max_attempts": cfg.agent.recovery_max_attempts,
        "skills_enabled": skills_enabled,
        "skills_global_dir": str(_DEFAULT_SKILLS) if skills_enabled else cfg.agent.skills_global_dir,
        "mcp_enabled": mcp_config.enabled,
        "mcp_server_ids": [server.id for server in mcp_config.servers if server.enabled],
    }
    with tempfile.TemporaryDirectory(prefix="forge-agent-eval-reference-") as temp_dir:
        reference = validate_suite_references(
            suite,
            temp_dir,
            source_repo_path=source_repo_path,
        )

    if not args.real_model:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason=args.reason,
            run_metadata=run_metadata,
        )
        print(json.dumps({"reference_validation": reference, "report": report}, ensure_ascii=False, indent=2))
        return 0

    if not cfg.llm.api_key:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason="provider_credentials_not_available",
            run_metadata=run_metadata,
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
            "strict_tool_schema": cfg.llm.strict_tool_schema,
        })
    except ValueError as exc:
        report = write_not_executed(
            suite,
            args.output_dir,
            variant=args.variant,
            repetitions=args.repetitions,
            task_ids=args.task,
            reason=f"provider_not_configured:{exc}",
            run_metadata=run_metadata,
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
            mcp_config,
            args.variant,
            args.total_token_budget,
        ),
        variant=args.variant,
        repetitions=args.repetitions,
        evidence_kind="real_model",
        real_model_executed=True,
        run_metadata=run_metadata,
        source_repo_path=source_repo_path,
    )
    results = harness.run(args.task)
    print(json.dumps({"trials": len(results), "output_dir": str(Path(args.output_dir).resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
