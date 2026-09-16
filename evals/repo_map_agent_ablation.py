"""Real Coding Agent ablation harness for Repo Map variants.

The harness intentionally uses the production ExecutionRunner, Agent completion
guards, tool registry, Trace v2, and hidden Independent Acceptance.  Validation
mode is provider-free and records that no real-model experiment was executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent.core import AgentConfig
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import EventType, Task
from config.schema import load_config
from entry.cli import _build_registry
from evals.harness import EvalCase, materialize_case, verify_case
from evals.run import _fixture_patch
from llm.router import create_backend_from_config


_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = Path(__file__).parent / "fixtures" / "repo_map_agent_cases.json"
_VARIANTS = (
    "no_repo_map",
    "static_repo_map",
    "query_aware_repo_map",
    "incremental_query_aware_repo_map",
)
_MODE_BY_VARIANT = {
    "no_repo_map": "none",
    "static_repo_map": "static",
    "query_aware_repo_map": "query_aware",
    "incremental_query_aware_repo_map": "incremental",
}


@dataclass(frozen=True)
class RepoMapAgentResult:
    case_id: str
    category: str
    variant: str
    repeat: int
    solved: bool
    agent_status: str
    acceptance_status: str
    verifier_passed: bool
    false_finish: bool
    termination_reason: str | None
    latency_seconds: float
    first_target_read_step: int | None
    files_read: int
    search_text_calls: int
    find_files_calls: int
    find_symbol_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    total_tokens: int
    repo_map_build_seconds: float
    repo_map_build_calls: int
    repo_map_full_rebuild_seconds: float
    repo_map_warm_load_seconds: float
    repo_map_incremental_update_seconds: float
    patch: str
    trace_path: str | None


def load_manifest(path: str | Path = _MANIFEST) -> tuple[dict[str, Any], list[EvalCase]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported Repo Map Agent manifest schema")
    defaults = dict(raw.get("defaults", {}))
    cases = [
        EvalCase(
            case_id=item["id"],
            category=item["category"],
            prompt=item["prompt"],
            files=dict(item["files"]),
            verifier=item["verifier"],
            target_files=tuple(item.get("target_files", ())),
            target_symbols=tuple(item.get("target_symbols", ())),
        )
        for item in raw.get("cases", ())
    ]
    if not cases:
        raise ValueError("Repo Map Agent manifest must contain cases")
    return defaults, cases


def validate_harness(
    *,
    manifest: str | Path = _MANIFEST,
    output: str | Path = "evals/results/repo_map_agent_ablation",
    reason: str = "provider_credentials_not_available_in_ci",
) -> dict[str, Any]:
    """Validate the frozen protocol without constructing or calling a provider."""
    manifest_path = Path(manifest).resolve()
    defaults, cases = load_manifest(manifest_path)
    metadata = _base_metadata(manifest_path, defaults, cases)
    metadata.update({
        "evidence_level": "Real-model Small Sample (infrastructure only; not executed)",
        "real_model_executed": False,
        "execution_status": "not_executed",
        "not_executed_reason": reason,
        "repetitions": 0,
        "run_count": 0,
    })
    report = {
        "execution_status": "not_executed",
        "real_model_executed": False,
        "reason": reason,
        "variants": list(_VARIANTS),
        "case_count": len(cases),
        "rows": 0,
        "claim_boundary": (
            "This artifact proves the production-path ablation harness and frozen "
            "fixtures exist. It does not provide Coding Agent success-rate evidence."
        ),
    }
    _write_outputs(Path(output), metadata, report, [])
    return {"metadata": metadata, "aggregate": report, "rows": 0}


def run_ablation(
    *,
    manifest: str | Path = _MANIFEST,
    output: str | Path = "evals/results/repo_map_agent_ablation",
    config_path: str | Path | None = None,
    repetitions: int = 1,
    case_ids: tuple[str, ...] = (),
    variants: tuple[str, ...] = (),
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    manifest_path = Path(manifest).resolve()
    defaults, cases = load_manifest(manifest_path)
    selected_cases = [case for case in cases if not case_ids or case.case_id in case_ids]
    selected_variants = [item for item in _VARIANTS if not variants or item in variants]
    missing_cases = sorted(set(case_ids) - {case.case_id for case in cases})
    missing_variants = sorted(set(variants) - set(_VARIANTS))
    if missing_cases:
        raise ValueError(f"unknown case(s): {', '.join(missing_cases)}")
    if missing_variants:
        raise ValueError(f"unknown variant(s): {', '.join(missing_variants)}")

    cfg = load_config(config_path)
    backend = create_backend_from_config({
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "api_key": cfg.llm.api_key or None,
        "base_url": cfg.llm.base_url or None,
        "max_tokens": cfg.llm.max_tokens,
    })
    output_dir = Path(output).resolve()
    rows: list[dict[str, Any]] = []
    raw_path = output_dir / "raw.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as stream:
        for repeat in range(1, repetitions + 1):
            for case in selected_cases:
                for variant in selected_variants:
                    row = _run_one(
                        case=case,
                        variant=variant,
                        repeat=repeat,
                        defaults=defaults,
                        cfg=cfg,
                        backend=backend,
                        output_dir=output_dir,
                    )
                    payload = asdict(row)
                    rows.append(payload)
                    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    stream.flush()

    metadata = _base_metadata(manifest_path, defaults, selected_cases)
    metadata.update({
        "evidence_level": "Real-model Small Sample",
        "real_model_executed": True,
        "execution_status": "executed",
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "repetitions": repetitions,
        "run_count": len(rows),
        "variants": selected_variants,
    })
    report = _aggregate(rows, selected_variants)
    report["execution_status"] = "executed"
    report["claim_boundary"] = (
        "Observed results are a small real-model sample. They are not a stable "
        "pass@1 estimate and must not be generalized beyond this fixture/model run."
    )
    _write_outputs(output_dir, metadata, report, rows, raw_already_written=True)
    return {"metadata": metadata, "aggregate": report, "rows": len(rows)}


def _run_one(
    *,
    case: EvalCase,
    variant: str,
    repeat: int,
    defaults: dict[str, Any],
    cfg,
    backend,
    output_dir: Path,
) -> RepoMapAgentResult:
    repo = materialize_case(
        case,
        output_dir / "repos" / variant / f"repeat-{repeat}" / case.case_id,
    )
    _init_fixture_repo(repo)
    registry = _build_registry(cfg, worktree_path=str(repo), workspace=str(repo))
    cache_dir = output_dir / "repo-map-cache" / variant / f"repeat-{repeat}" / case.case_id
    agent_config = AgentConfig(
        max_steps=int(defaults.get("max_steps", 12)),
        budget_tokens=int(defaults.get("budget_tokens", 40_000)),
        history_max_messages=int(defaults.get("history_max_messages", 40)),
        stream=False,
        repo_map_mode=_MODE_BY_VARIANT[variant],
        repo_map_cache_dir=str(cache_dir),
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=agent_config,
        log_dir=str(output_dir / "traces" / variant / f"repeat-{repeat}" / case.case_id),
    )
    task = Task(
        case.prompt,
        str(repo),
        task_id=f"repo-map-{variant}-{repeat}-{case.case_id}",
        max_steps=agent_config.max_steps,
        budget_tokens=agent_config.budget_tokens,
        require_changes=True,
        require_tests=bool(defaults.get("require_tests", False)),
    )

    def hidden_verifier(workspace: Path) -> bool:
        return verify_case(case, workspace).returncode == 0

    acceptance = AcceptanceContract(
        require_changes=True,
        require_tests=task.require_tests,
        verifier=hidden_verifier,
    )
    started = time.perf_counter()
    result = runner.run(
        RunRequest(task=task, acceptance=acceptance, entrypoint="cli"),
    )
    elapsed = time.perf_counter() - started
    verifier_passed = verify_case(case, repo).returncode == 0
    exploration = trace_exploration_metrics(result.trace_path, repo, case.target_files)
    telemetry = runner.agent.repo_map_telemetry
    index = telemetry.get("index") or {}
    solved = result.is_success() and result.acceptance_status == "passed" and verifier_passed
    return RepoMapAgentResult(
        case_id=case.case_id,
        category=case.category,
        variant=variant,
        repeat=repeat,
        solved=solved,
        agent_status=result.status.value,
        acceptance_status=result.acceptance_status,
        verifier_passed=verifier_passed,
        false_finish=result.is_success() and not verifier_passed,
        termination_reason=result.termination_reason,
        latency_seconds=elapsed,
        first_target_read_step=exploration["first_target_read_step"],
        files_read=exploration["files_read"],
        search_text_calls=exploration["search_text_calls"],
        find_files_calls=exploration["find_files_calls"],
        find_symbol_calls=exploration["find_symbol_calls"],
        tool_calls=exploration["tool_calls"],
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cached_input_tokens=result.usage.cached_tokens,
        cache_write_tokens=result.usage.cache_write_tokens,
        total_tokens=result.total_tokens,
        repo_map_build_seconds=float(telemetry.get("build_seconds", 0.0)),
        repo_map_build_calls=int(telemetry.get("build_calls", 0)),
        repo_map_full_rebuild_seconds=sum(float(x) for x in index.get("full_rebuild_seconds", ())),
        repo_map_warm_load_seconds=sum(float(x) for x in index.get("warm_load_seconds", ())),
        repo_map_incremental_update_seconds=sum(
            float(x) for x in index.get("incremental_update_seconds", ())
        ),
        patch=_fixture_patch(case, repo),
        trace_path=result.trace_path,
    )


def trace_exploration_metrics(
    trace_path: str | None,
    repo: str | Path,
    target_files: tuple[str, ...],
) -> dict[str, int | None]:
    metrics: dict[str, int | None] = {
        "first_target_read_step": None,
        "files_read": 0,
        "search_text_calls": 0,
        "find_files_calls": 0,
        "find_symbol_calls": 0,
        "tool_calls": 0,
    }
    if not trace_path:
        return metrics
    root = Path(repo).resolve()
    targets = {Path(path).as_posix() for path in target_files}
    read_files: set[str] = set()
    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event_type") != EventType.ACTION.value:
            continue
        payload = event.get("payload", {})
        action = payload.get("action") or {}
        call = action.get("tool_call") or {}
        tool = call.get("name")
        if not tool:
            continue
        metrics["tool_calls"] = int(metrics["tool_calls"] or 0) + 1
        if tool == "search_text":
            metrics["search_text_calls"] = int(metrics["search_text_calls"] or 0) + 1
        elif tool == "find_files":
            metrics["find_files_calls"] = int(metrics["find_files_calls"] or 0) + 1
        elif tool == "find_symbol":
            metrics["find_symbol_calls"] = int(metrics["find_symbol_calls"] or 0) + 1
        if tool not in {"file_read", "file_view"}:
            continue
        raw_path = (call.get("params") or {}).get("path")
        relative = _relative_tool_path(raw_path, root)
        if relative is None:
            continue
        read_files.add(relative)
        if relative in targets and metrics["first_target_read_step"] is None:
            metrics["first_target_read_step"] = int(payload.get("step") or 0)
    metrics["files_read"] = len(read_files)
    return metrics


def _relative_tool_path(raw_path: object, root: Path) -> str | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    try:
        absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
        return absolute.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _aggregate(rows: list[dict[str, Any]], variants: list[str] | tuple[str, ...]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    for variant in variants:
        group = [row for row in rows if row["variant"] == variant]
        if not group:
            continue
        target_steps = [
            int(row["first_target_read_step"])
            for row in group
            if row.get("first_target_read_step") is not None
        ]
        aggregate[variant] = {
            "runs": len(group),
            "observed_solved": sum(bool(row["solved"]) for row in group),
            "observed_verifier_passes": sum(bool(row["verifier_passed"]) for row in group),
            "observed_false_finishes": sum(bool(row["false_finish"]) for row in group),
            "mean_first_target_read_step": statistics.mean(target_steps) if target_steps else None,
            "mean_files_read": statistics.mean(int(row["files_read"]) for row in group),
            "mean_search_text_calls": statistics.mean(int(row["search_text_calls"]) for row in group),
            "mean_find_files_calls": statistics.mean(int(row["find_files_calls"]) for row in group),
            "mean_input_tokens": statistics.mean(int(row["input_tokens"]) for row in group),
            "mean_total_tokens": statistics.mean(int(row["total_tokens"]) for row in group),
            "mean_cached_input_tokens": statistics.mean(
                int(row["cached_input_tokens"]) for row in group
            ),
            "mean_latency_seconds": statistics.mean(float(row["latency_seconds"]) for row in group),
            "mean_repo_map_build_seconds": statistics.mean(
                float(row["repo_map_build_seconds"]) for row in group
            ),
            "mean_repo_map_incremental_update_seconds": statistics.mean(
                float(row["repo_map_incremental_update_seconds"]) for row in group
            ),
        }
    return {"variants": aggregate, "run_count": len(rows)}


def _base_metadata(
    manifest_path: Path,
    defaults: dict[str, Any],
    cases: list[EvalCase],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runner_revision": _git_revision(_ROOT),
        "fixture_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "case_count": len(cases),
        "cases": [case.case_id for case in cases],
        "variants": list(_VARIANTS),
        "max_steps": int(defaults.get("max_steps", 12)),
        "budget_tokens": int(defaults.get("budget_tokens", 40_000)),
        "require_tests": bool(defaults.get("require_tests", False)),
    }


def _write_outputs(
    output_dir: Path,
    metadata: dict[str, Any],
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    raw_already_written: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not raw_already_written:
        with (output_dir / "raw.jsonl").open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Repo Map Real Coding Agent Ablation",
        "",
        f"- Execution status: `{metadata['execution_status']}`",
        f"- Real model executed: `{metadata['real_model_executed']}`",
        f"- Cases: `{metadata['case_count']}`",
        f"- Variants: `{', '.join(metadata['variants'])}`",
        "",
    ]
    if not metadata["real_model_executed"]:
        lines += [
            "## Result",
            "",
            f"Real-model runs were not executed: `{metadata['not_executed_reason']}`.",
            "",
            "The deterministic fixture and production-path harness are ready, but this "
            "artifact contains no solved-rate, token, latency, or cache-hit claim.",
        ]
    else:
        lines += [
            "## Observed small-sample results",
            "",
            "| variant | runs | solved | verifier pass | mean input tokens | mean total tokens | mean latency (s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for variant, values in report["variants"].items():
            lines.append(
                f"| {variant} | {values['runs']} | {values['observed_solved']} | "
                f"{values['observed_verifier_passes']} | {values['mean_input_tokens']:.1f} | "
                f"{values['mean_total_tokens']:.1f} | {values['mean_latency_seconds']:.3f} |"
            )
        lines += [
            "",
            "These are observed Real-model Small Sample values, not stable pass@1 estimates.",
        ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _init_fixture_repo(repo: Path) -> None:
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "forge-agent-eval@example.invalid"),
        ("git", "config", "user.name", "Forge Agent Eval"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "fixture baseline"),
    )
    for command in commands:
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)


def _git_revision(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(_MANIFEST))
    parser.add_argument("--output", default="evals/results/repo_map_agent_ablation")
    parser.add_argument("--config")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--variant", action="append", default=[])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--reason", default="provider_credentials_not_available_in_ci")
    args = parser.parse_args()
    if args.validate_only:
        result = validate_harness(
            manifest=args.manifest,
            output=args.output,
            reason=args.reason,
        )
    else:
        result = run_ablation(
            manifest=args.manifest,
            output=args.output,
            config_path=args.config,
            repetitions=args.repetitions,
            case_ids=tuple(args.case),
            variants=tuple(args.variant),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
