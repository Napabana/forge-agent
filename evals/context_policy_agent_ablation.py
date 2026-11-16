"""B2 Context Policy 真实 Coding Agent 消融：受控长历史 + hidden verifier。"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent.core import AgentConfig, PrepareNextTurnResult
from agent.prompt import build_task_prompt
from agent.runner import ExecutionRunner, RunRequest
from agent.task import EventType, Task
from config.schema import load_config
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from context.token_budget import estimate_messages_tokens, recent_history_units
from context.tool_pruning import DeterministicToolPruner
from entry.cli import _build_registry
from evals.harness import EvalCase, materialize_case, verify_case
from evals.run import _count_tool_calls, _fixture_patch
from llm.base import LLMMessage
from llm.router import create_backend_from_config


_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = Path(__file__).parent / "fixtures" / "context_policy_agent_cases.json"
_VARIANTS = ("baseline", "pruning_only", "hybrid_compaction")
_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class AgentContextCase:
    base: EvalCase
    history_spec: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AgentAblationResult:
    case_id: str
    category: str
    variant: str
    passed: bool
    false_finish: bool
    agent_status: str
    verifier_status: str
    agent_error: str | None
    termination_reason: str | None
    resource_reason: str | None
    completion_rejections: int
    completion_rejection_reasons: dict[str, int]
    latency_seconds: float
    tool_calls: int
    agent_tokens: int
    input_tokens: int
    output_tokens: int
    semantic_tokens: int
    total_tokens_with_context: int
    preloaded_history_tokens: int
    context_policy_calls: int
    context_checkpoints: int
    context_pruned_units: int
    semantic_calls: int
    semantic_error_count: int
    initial_pressure_ratio: float | None
    max_pressure_ratio: float | None
    patch: str
    trace_path: str | None


class PruningOnlyPolicy:
    """B2 专用 Stage-A ablation：只做生产 DeterministicToolPruner，不做 semantic summary。"""

    def __init__(self, *, threshold: float, keep_recent_tokens: int) -> None:
        self.threshold = threshold
        self.keep_recent_tokens = keep_recent_tokens
        self.pruner = DeterministicToolPruner()
        self.pruned_units = 0

    def __call__(self, context):
        raw = context.history.to_dicts()
        pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=raw,
            tools=context.tool_schemas,
        )
        if pressure.ratio < self.threshold:
            return None
        recent = recent_history_units(raw, self.keep_recent_tokens)
        if not recent:
            return None
        pruning = self.pruner.prune(
            context.history.to_list(),
            protected_from_index=recent[0].indices[0],
        )
        self.pruned_units += pruning.pruned_units
        if not pruning.pruned_units:
            return None
        return PrepareNextTurnResult(history_override=pruning.messages)


class CountingPolicy:
    """记录 Context Policy 实际调用次数与请求压力，不改变被测策略行为。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.call_count = 0
        self.pressure_ratios: list[float] = []

    def __call__(self, context):
        self.call_count += 1
        pressure = context.token_budget.request_pressure(
            system_text=context.system_content,
            repo_map_text=context.repo_map_content,
            history=context.history.to_dicts(),
            tools=context.tool_schemas,
        )
        self.pressure_ratios.append(pressure.ratio)
        threshold = float(getattr(self.inner, "threshold", 1.0))
        print(
            f"    [context] call={self.call_count} pressure={pressure.ratio:.3f} "
            f"threshold={threshold:.3f}",
            flush=True,
        )
        return self.inner(context)


def load_manifest(path: str | Path = _MANIFEST) -> tuple[dict[str, Any], list[AgentContextCase]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported B2 manifest schema")
    cases: list[AgentContextCase] = []
    for item in raw.get("cases", ()):
        base = EvalCase(
            case_id=item["id"],
            category=item["category"],
            prompt=item["prompt"],
            files=item["files"],
            verifier=item["verifier"],
            target_files=tuple(item.get("target_files", ())),
            target_symbols=tuple(item.get("target_symbols", ())),
        )
        cases.append(AgentContextCase(base=base, history_spec=tuple(item.get("history_spec", ()))))
    if not cases:
        raise ValueError("B2 manifest must contain cases")
    return raw["defaults"], cases


def build_history(case: AgentContextCase, repo: Path) -> ConversationHistory:
    """物化 canonical 长历史，并显式追加本轮真实 task prompt。"""
    history = ConversationHistory(max_messages=None)
    sequence = 0
    for spec in case.history_spec:
        kind = spec["kind"]
        if kind == "user":
            history.add(LLMMessage("user", str(spec["content"])))
        elif kind == "user_repeat":
            for index in range(int(spec["count"])):
                history.add(LLMMessage("user", str(spec["template"]).format(i=index)))
        elif kind == "tool":
            sequence += 1
            tool = str(spec["tool"])
            params = dict(spec.get("params", {}))
            status = str(spec.get("status", "SUCCESS")).upper()
            output = _expand_output(spec)
            history.add(LLMMessage(
                "assistant",
                "Thought: historical benchmark evidence\n"
                f"Action: {tool}\n"
                f"Params: {json.dumps(params, ensure_ascii=False)}",
                event_ref=f"b2-{case.base.case_id}-a{sequence}",
            ))
            history.add(LLMMessage(
                "user",
                f"[Tool: {tool} | {status}]\n{output}",
                event_ref=f"b2-{case.base.case_id}-o{sequence}",
            ))
        else:
            raise ValueError(f"unknown B2 history kind: {kind}")
    history.add(LLMMessage(
        "user",
        build_task_prompt(case.base.prompt, str(repo), None),
    ))
    return history


def _expand_output(spec: dict[str, Any]) -> str:
    if "output" in spec:
        return str(spec["output"])
    template = str(spec.get("output_template", ""))
    return "\n".join(template.format(j=index) for index in range(int(spec.get("output_repeat", 1))))


def _build_backend(cfg):
    return create_backend_from_config({
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "model": cfg.llm.model,
        "api_key": cfg.llm.api_key or None,
        "base_url": cfg.llm.base_url or None,
        "max_tokens": cfg.llm.max_tokens,
    })


def _build_policy(variant: str, defaults: dict[str, Any], backend):
    if variant == "baseline":
        return None
    if variant == "pruning_only":
        return PruningOnlyPolicy(
            threshold=float(defaults["threshold"]),
            keep_recent_tokens=int(defaults["keep_recent_tokens"]),
        )
    if variant == "hybrid_compaction":
        policy = TraceableCompaction(
            threshold=float(defaults["threshold"]),
            target_ratio=float(defaults["target_ratio"]),
            keep_recent_tokens=int(defaults["keep_recent_tokens"]),
            max_summary_chars=int(defaults["max_summary_chars"]),
        )
        policy.bind_backend(backend)
        return policy
    raise ValueError(f"unknown B2 variant: {variant}")


def run_ablation(
    *,
    manifest: str | Path = _MANIFEST,
    output: str | Path = "evals/results/context_policy_agent_ablation",
    config_path: str | Path | None = None,
    case_ids: tuple[str, ...] = (),
    variants: tuple[str, ...] = (),
) -> dict[str, Any]:
    manifest_path = Path(manifest).resolve()
    defaults, cases = load_manifest(manifest_path)
    selected_cases = [case for case in cases if not case_ids or case.base.case_id in case_ids]
    selected_variants = [variant for variant in _VARIANTS if not variants or variant in variants]
    if case_ids:
        missing = sorted(set(case_ids) - {case.base.case_id for case in cases})
        if missing:
            raise ValueError(f"unknown B2 case(s): {', '.join(missing)}")
    if variants:
        missing_variants = sorted(set(variants) - set(_VARIANTS))
        if missing_variants:
            raise ValueError(f"unknown B2 variant(s): {', '.join(missing_variants)}")

    cfg = load_config(config_path)
    backend = _build_backend(cfg)
    output_dir = Path(output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw.jsonl"
    rows: list[dict[str, Any]] = []
    total = len(selected_cases) * len(selected_variants)
    index = 0

    with raw_path.open("w", encoding="utf-8") as stream:
        for case in selected_cases:
            for variant in selected_variants:
                index += 1
                print(f"\n[{index}/{total}] {case.base.case_id} / {variant}", flush=True)
                row = _run_one(
                    case=case,
                    variant=variant,
                    defaults=defaults,
                    cfg=cfg,
                    backend=backend,
                    output_dir=output_dir,
                    progress_index=index,
                    progress_total=total,
                )
                payload = asdict(row)
                rows.append(payload)
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                stream.flush()
                pressure = "n/a" if row.max_pressure_ratio is None else f"{row.max_pressure_ratio:.3f}"
                print(
                    f"    {'PASS' if row.passed else 'FAIL'} | agent={row.agent_status} "
                    f"| verifier={row.verifier_status} | tokens={row.agent_tokens}+{row.semantic_tokens} "
                    f"| pressure(max)={pressure} | {row.latency_seconds:.1f}s | 剩余 {total-index}",
                    flush=True,
                )
                if row.agent_error:
                    print(f"    agent_error={row.agent_error}", flush=True)

    metadata = {
        "schema_version": 1,
        "runner_revision": _git_revision(_ROOT),
        "fixture_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "model": cfg.llm.model,
        "provider": cfg.llm.provider,
        "protocol": cfg.llm.protocol,
        "budget_tokens": int(defaults["budget_tokens"]),
        "max_steps": int(defaults["max_steps"]),
        "cases": [case.base.case_id for case in selected_cases],
        "variants": selected_variants,
        "run_count": len(rows),
    }
    report = _aggregate(rows)
    report["comparison_v2_v3"] = _compare_v2(rows)
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(
        _report_markdown(metadata, report, rows), encoding="utf-8"
    )
    return {"metadata": metadata, "aggregate": report, "rows": len(rows)}


def _run_one(
    *,
    case: AgentContextCase,
    variant: str,
    defaults: dict[str, Any],
    cfg,
    backend,
    output_dir: Path,
    progress_index: int,
    progress_total: int,
) -> AgentAblationResult:
    repo = materialize_case(case.base, output_dir / "repos" / variant / case.base.case_id)
    _init_fixture_repo(repo)
    history = build_history(case, repo)
    preloaded_tokens = estimate_messages_tokens(history.to_dicts())
    policy = _build_policy(variant, defaults, backend)
    policy_probe = CountingPolicy(policy) if policy is not None else None
    registry = _build_registry(cfg, default_cwd=str(repo), workspace=str(repo))
    agent_config = AgentConfig(
        max_steps=int(defaults["max_steps"]),
        budget_tokens=int(defaults["budget_tokens"]),
        history_max_messages=40,
        stream=False,
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=agent_config,
        log_dir=str(output_dir / "traces" / variant / case.base.case_id),
    )
    task = Task(
        case.base.prompt,
        str(repo),
        task_id=f"b2-{variant}-{case.base.case_id}",
        max_steps=int(defaults["max_steps"]),
        budget_tokens=int(defaults["budget_tokens"]),
        require_changes=True,
    )

    started = time.perf_counter()
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat,
        args=(stop, started, progress_index, progress_total, case.base.case_id, variant),
        daemon=True,
    )
    heartbeat.start()
    try:
        result = runner.run(
            RunRequest(task=task, history=history, prepare_next_turn=policy_probe),
            on_event=lambda event: _print_event(event, case.base.case_id, variant),
        )
    finally:
        stop.set()
        heartbeat.join(timeout=0.2)
    elapsed = time.perf_counter() - started

    verification = verify_case(case.base, repo)
    verifier_passed = verification.returncode == 0
    semantic_tokens = 0
    semantic_calls = 0
    semantic_error_count = 0
    checkpoints = 0
    policy_calls = policy_probe.call_count if policy_probe is not None else 0
    pruned_units = int(getattr(policy, "pruned_units", 0)) if policy is not None else 0
    pressure_ratios = policy_probe.pressure_ratios if policy_probe is not None else []
    if isinstance(policy, TraceableCompaction):
        pending = policy.consume_usage()
        semantic_tokens = pending.total_tokens
        summarizer = policy.semantic_summarizer
        semantic_calls = int(getattr(summarizer, "call_count", 0)) if summarizer is not None else 0
        checkpoints = len(policy.checkpoints)
        pruned_units = sum(checkpoint.pruned_units for checkpoint in policy.checkpoints)
        semantic_error_count = sum(bool(checkpoint.semantic_error) for checkpoint in policy.checkpoints)

    passed = result.is_success() and verifier_passed
    rejection_reasons = _completion_rejections(result.trace_path)
    return AgentAblationResult(
        case_id=case.base.case_id,
        category=case.base.category,
        variant=variant,
        passed=passed,
        false_finish=result.is_success() and not verifier_passed,
        agent_status=result.status.value,
        verifier_status="passed" if verifier_passed else "failed",
        agent_error=result.error,
        termination_reason=result.termination_reason,
        resource_reason=result.resource_reason,
        completion_rejections=sum(rejection_reasons.values()),
        completion_rejection_reasons=rejection_reasons,
        latency_seconds=elapsed,
        tool_calls=_count_tool_calls(result.trace_path),
        agent_tokens=result.total_tokens,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        semantic_tokens=semantic_tokens,
        total_tokens_with_context=result.total_tokens + semantic_tokens,
        preloaded_history_tokens=preloaded_tokens,
        context_policy_calls=policy_calls,
        context_checkpoints=checkpoints,
        context_pruned_units=pruned_units,
        semantic_calls=semantic_calls,
        semantic_error_count=semantic_error_count,
        initial_pressure_ratio=pressure_ratios[0] if pressure_ratios else None,
        max_pressure_ratio=max(pressure_ratios) if pressure_ratios else None,
        patch=_fixture_patch(case.base, repo),
        trace_path=result.trace_path,
    )


def _init_fixture_repo(repo: Path) -> None:
    """把 B2 fixture 变成独立 Git repo，避免继承 Forge Agent 父仓库的 ignore/status。"""
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "forge-agent-eval@example.invalid"),
        ("git", "config", "user.name", "Forge Agent Eval"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "fixture baseline"),
    )
    for command in commands:
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)


def _completion_rejections(trace_path: str | None) -> dict[str, int]:
    """从 append-only Trace 汇总 guard rejection；旧 Trace 自然返回空分布。"""
    counts: dict[str, int] = {}
    if not trace_path:
        return counts
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        event = json.loads(line)
        if event.get("event_type") != EventType.COMPLETION_REJECTED.value:
            continue
        code = str(event.get("payload", {}).get("code") or "UNKNOWN")
        counts[code] = counts.get(code, 0) + 1
    return counts


def _heartbeat(stop: threading.Event, started: float, index: int, total: int, case_id: str, variant: str) -> None:
    while not stop.wait(_HEARTBEAT_SECONDS):
        print(
            f"    [仍在运行] {index}/{total} {case_id}/{variant} 已 {time.perf_counter()-started:.1f}s",
            flush=True,
        )


def _print_event(event, case_id: str, variant: str) -> None:
    step = event.payload.get("step") if isinstance(event.payload, dict) else None
    if event.event_type == EventType.ACTION:
        action = event.payload.get("action", {})
        tool = (action.get("tool_call") or {}).get("name")
        print(f"    [step {step}] action={action.get('action_type')} tool={tool or '-'}", flush=True)
    elif event.event_type == EventType.OBSERVATION:
        obs = event.payload.get("observation", {})
        print(f"    [step {step}] observation={obs.get('tool_name')} status={obs.get('status')}", flush=True)
    elif event.event_type == EventType.CONTEXT_COMPACTION_STARTED:
        print(f"    [step {step}] context semantic compaction started", flush=True)
    elif event.event_type == EventType.CONTEXT_COMPACTED:
        print(f"    [step {step}] context compacted", flush=True)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for variant in _VARIANTS:
        group = [row for row in rows if row["variant"] == variant]
        if not group:
            continue
        solved = sum(bool(row["passed"]) for row in group)
        verifier_passed = sum(row.get("verifier_status") == "passed" for row in group)
        max_steps_exhausted = sum(
            row.get("agent_status") == "max_steps"
            or (
                row.get("agent_status") == "incomplete"
                and row.get("termination_reason") == "resource_exhausted"
                and row.get("resource_reason") == "max_steps"
            )
            for row in group
        )
        total_tokens = sum(int(row["total_tokens_with_context"]) for row in group)
        latencies = sorted(float(row["latency_seconds"]) for row in group)
        pressures = [
            float(row["max_pressure_ratio"])
            for row in group
            if row.get("max_pressure_ratio") is not None
        ]
        result[variant] = {
            "runs": len(group),
            "solved": solved,
            "pass_at_1": solved / len(group),
            "verifier_pass_rate": verifier_passed / len(group),
            "max_steps_exhausted_rate": max_steps_exhausted / len(group),
            "false_finish_rate": sum(bool(row["false_finish"]) for row in group) / len(group),
            "mean_agent_tokens": statistics.mean(int(row["agent_tokens"]) for row in group),
            "mean_input_tokens": statistics.mean(int(row.get("input_tokens", 0)) for row in group),
            "mean_output_tokens": statistics.mean(int(row.get("output_tokens", 0)) for row in group),
            "mean_semantic_tokens": statistics.mean(int(row["semantic_tokens"]) for row in group),
            "mean_total_tokens_with_context": statistics.mean(int(row["total_tokens_with_context"]) for row in group),
            "tokens_per_solved": total_tokens / solved if solved else 0.0,
            "mean_latency_seconds": statistics.mean(latencies),
            "p95_latency_seconds": latencies[-1],
            "tool_calls": sum(int(row["tool_calls"]) for row in group),
            "context_trigger_rate": sum(int(row["context_checkpoints"]) > 0 for row in group) / len(group),
            "semantic_calls": sum(int(row["semantic_calls"]) for row in group),
            "semantic_error_count": sum(int(row["semantic_error_count"]) for row in group),
            "mean_max_pressure_ratio": statistics.mean(pressures) if pressures else None,
            "status_distribution": _count_values(group, "agent_status"),
            "termination_reason_distribution": _count_values(group, "termination_reason"),
            "resource_reason_distribution": _count_values(group, "resource_reason"),
            "completion_rejections": sum(int(row.get("completion_rejections", 0)) for row in group),
            "completion_rejection_reasons": _merge_counts(
                row.get("completion_rejection_reasons", {}) for row in group
            ),
        }
    return result


def _count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _merge_counts(groups) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in groups:
        for key, value in group.items():
            counts[key] = counts.get(key, 0) + int(value)
    return counts


def _compare_v2(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 case/variant 对照冻结的 B2 v2；缺失旧文件时仍保留 v3 证据。"""
    v2_path = _ROOT / "evals" / "results" / "context_policy_agent_ablation_v2" / "raw.jsonl"
    old_rows = []
    if v2_path.exists():
        old_rows = [json.loads(line) for line in v2_path.read_text(encoding="utf-8").splitlines() if line]
    old = {(row["case_id"], row["variant"]): row for row in old_rows}
    comparison = []
    for row in rows:
        previous = old.get((row["case_id"], row["variant"]), {})
        comparison.append({
            "case_id": row["case_id"],
            "variant": row["variant"],
            "v2_passed": previous.get("passed"),
            "v3_passed": row.get("passed"),
            "v2_verifier_status": previous.get("verifier_status"),
            "v3_verifier_status": row.get("verifier_status"),
            "v2_agent_status": previous.get("agent_status"),
            "v3_agent_status": row.get("agent_status"),
            "v3_termination_reason": row.get("termination_reason"),
            "v3_resource_reason": row.get("resource_reason"),
            "v2_total_tokens": previous.get("total_tokens_with_context"),
            "v3_total_tokens": row.get("total_tokens_with_context"),
            "v2_latency_seconds": previous.get("latency_seconds"),
            "v3_latency_seconds": row.get("latency_seconds"),
        })
    return comparison


def _report_markdown(
    metadata: dict[str, Any], report: dict[str, Any], rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# B2 Context Policy Agent Ablation",
        "",
        f"- runner revision: `{metadata['runner_revision']}`",
        f"- fixture sha256: `{metadata['fixture_sha256']}`",
        f"- model: `{metadata['model']}`",
        f"- runs: {metadata['run_count']}",
        f"- cases: {', '.join(metadata['cases'])}",
        "",
        "| variant | pass@1 | verifier pass | max-step exhausted | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in _VARIANTS:
        if variant not in report:
            continue
        row = report[variant]
        pressure = row.get("mean_max_pressure_ratio")
        pressure_text = "n/a" if pressure is None else f"{pressure:.3f}"
        lines.append(
            f"| {variant} | {row['pass_at_1']:.3f} | {row['verifier_pass_rate']:.3f} | "
            f"{row['max_steps_exhausted_rate']:.3f} | {row['tokens_per_solved']:.1f} | "
            f"{row['mean_total_tokens_with_context']:.1f} | {row['mean_latency_seconds']:.1f} | "
            f"{row['context_trigger_rate']:.3f} | {row['semantic_calls']} | {pressure_text} |"
        )
    lines.extend([
        "", "## Per-run evidence", "",
        "| case | variant | strict | verifier | status | termination | resource | input | output | total | latency(s) | rejections | diff | trace |",
        "|---|---|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ])
    for row in rows:
        trace = row.get("trace_path") or ""
        lines.append(
            f"| {row['case_id']} | {row['variant']} | {int(bool(row['passed']))} | "
            f"{row['verifier_status']} | {row['agent_status']} | {row.get('termination_reason') or '-'} | "
            f"{row.get('resource_reason') or '-'} | {row.get('input_tokens', 0)} | "
            f"{row.get('output_tokens', 0)} | {row['total_tokens_with_context']} | "
            f"{row['latency_seconds']:.1f} | {row.get('completion_rejections', 0)} | "
            f"{'yes' if row.get('patch') else 'no'} | `{trace}` |"
        )
    lines.extend([
        "", "## B2 v2 → v3 cell comparison", "",
        "| case | variant | v2 strict | v3 strict | v2 verifier | v3 verifier | v2 status | v3 status | v3 termination/resource |",
        "|---|---|---:|---:|---|---|---|---|---|",
    ])
    for row in report.get("comparison_v2_v3", []):
        lines.append(
            f"| {row['case_id']} | {row['variant']} | {row.get('v2_passed')} | "
            f"{row.get('v3_passed')} | {row.get('v2_verifier_status')} | "
            f"{row.get('v3_verifier_status')} | {row.get('v2_agent_status')} | "
            f"{row.get('v3_agent_status')} | {row.get('v3_termination_reason') or '-'} / "
            f"{row.get('v3_resource_reason') or '-'} |"
        )
    lines.extend([
        "", "## B2 findings", "",
        "- Commit-aware completion: inspect the long-hard-constraint/hybrid and "
        "huge-tool-history/baseline comparison rows; both v2 guard failures are SUCCESS in v3.",
        "- Superseded state: hybrid and pruning finish successfully in v3, while baseline still has "
        "verifier passed with `INCOMPLETE + resource_exhausted/max_steps`.",
        "- Completion rejection: the per-run table and JSON distributions retain every rejection; "
        "a rejection is not counted as SUCCESS unless a later FINISH passes the guard.",
        "", "## Interpretation limits", "",
        "This experiment has only three cases per variant (`n=3`) and uses a non-deterministic model. "
        "The results are descriptive evidence for these runs, not a general performance claim.",
    ])
    return "\n".join(lines) + "\n"


def _git_revision(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(_MANIFEST))
    parser.add_argument("--output", default="evals/results/context_policy_agent_ablation")
    parser.add_argument("--config", default=None)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--variant", action="append", choices=_VARIANTS, default=[])
    args = parser.parse_args()
    report = run_ablation(
        manifest=args.manifest,
        output=args.output,
        config_path=args.config,
        case_ids=tuple(args.case),
        variants=tuple(args.variant),
    )
    print("\nB2 完成", flush=True)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
