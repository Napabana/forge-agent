"""Context Policy benchmark 的可观察 CLI；不改变 benchmark 计算逻辑。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

from context.token_budget import TokenBudget
from evals import context_policy_benchmark as benchmark


_ORIGINAL_EVALUATE = benchmark._evaluate_case_variant
_ORIGINAL_TRIM_HISTORY = TokenBudget.trim_history
_ORIGINAL_LOAD_MANIFEST = benchmark.load_manifest
_HEARTBEAT_SECONDS = 10.0
_PLAN: list[tuple[str, str]] = []
_CASE_FILTER: tuple[str, ...] = ()
_VARIANT_FILTER: tuple[str, ...] = ()
_STATE: dict[str, Any] = {
    "index": 0,
    "current": None,
    "current_started": None,
    "trim_calls": 0,
}


def _extract_filter_args() -> None:
    """提取 progress CLI 自己的筛选参数，并从 argv 中移除后再交给原 benchmark CLI。"""
    global _CASE_FILTER, _VARIANT_FILTER
    cases: list[str] = []
    variants: list[str] = []
    forwarded = [sys.argv[0]]
    index = 1
    while index < len(sys.argv):
        arg = sys.argv[index]
        if arg in {"--case", "--variant"}:
            if index + 1 >= len(sys.argv):
                raise SystemExit(f"{arg} requires a value")
            value = sys.argv[index + 1]
            if arg == "--case":
                cases.append(value)
            else:
                variants.append(value)
            index += 2
            continue
        forwarded.append(arg)
        index += 1
    unknown_variants = sorted(set(variants) - set(benchmark._VARIANTS))
    if unknown_variants:
        raise SystemExit(f"unknown --variant: {', '.join(unknown_variants)}")
    _CASE_FILTER = tuple(dict.fromkeys(cases))
    _VARIANT_FILTER = tuple(dict.fromkeys(variants))
    sys.argv[:] = forwarded


def _manifest_path_from_argv() -> Path:
    """只读取 CLI 中的 --manifest；其余参数继续交给原 benchmark.main()。"""
    for index, arg in enumerate(sys.argv[:-1]):
        if arg == "--manifest":
            return Path(sys.argv[index + 1]).resolve()
    return benchmark._MANIFEST


def _filtered_manifest(path: str | Path = benchmark._MANIFEST) -> dict[str, Any]:
    """按 case/variant 过滤 manifest；fixture 本身不修改，便于做低成本 pilot。"""
    data = _ORIGINAL_LOAD_MANIFEST(path)
    available_cases = {str(case["id"]) for case in data["cases"]}
    unknown_cases = sorted(set(_CASE_FILTER) - available_cases)
    if unknown_cases:
        raise ValueError(f"unknown benchmark case: {', '.join(unknown_cases)}")

    selected_cases: list[dict[str, Any]] = []
    for original in data["cases"]:
        case_id = str(original["id"])
        if _CASE_FILTER and case_id not in _CASE_FILTER:
            continue
        case = dict(original)
        variants = tuple(case.get("applicable_variants", benchmark._VARIANTS))
        if _VARIANT_FILTER:
            variants = tuple(variant for variant in variants if variant in _VARIANT_FILTER)
        if not variants:
            continue
        case["applicable_variants"] = list(variants)
        selected_cases.append(case)

    if not selected_cases:
        raise ValueError("benchmark filters selected no case-variant runs")
    return {**data, "cases": selected_cases}


def _build_plan() -> list[tuple[str, str]]:
    data = _filtered_manifest(_manifest_path_from_argv())
    plan: list[tuple[str, str]] = []
    for case in data["cases"]:
        for variant in case.get("applicable_variants", benchmark._VARIANTS):
            plan.append((str(case["id"]), str(variant)))
    return plan


def _print(message: str = "") -> None:
    print(message, flush=True)


def _stage_hint(variant: str) -> str:
    if variant == "budget_trim_only":
        return "物化 History -> baseline view -> final TokenBudget trim -> metrics"
    if variant == "deterministic_pruning":
        return "物化 History -> Stage A deterministic pruning -> final TokenBudget trim -> metrics"
    return "物化 History -> Hybrid Stage A/Stage B -> final TokenBudget trim -> metrics"


def _heartbeat(stop: threading.Event, started: float, case_id: str, variant: str) -> None:
    """长时间 trim 时周期输出心跳，避免命令行长时间无反馈。"""
    while not stop.wait(_HEARTBEAT_SECONDS):
        elapsed = time.perf_counter() - started
        _print(
            f"    [仍在计算] TokenBudget.trim_history() 已运行 {elapsed:.1f}s "
            f"| {case_id}/{variant}"
        )


def _trim_history_with_progress(self: TokenBudget, messages: list[dict], token_limit: int) -> list[dict]:
    current = _STATE.get("current")
    if current is not None:
        _STATE["trim_calls"] = int(_STATE["trim_calls"]) + 1
        case_id, variant = current
        _print(
            f"    [阶段] final TokenBudget.trim_history() 开始 "
            f"| messages={len(messages)} | limit={token_limit} | {case_id}/{variant}"
        )
        started = time.perf_counter()
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat,
            args=(stop, started, case_id, variant),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = _ORIGINAL_TRIM_HISTORY(self, messages, token_limit)
        except KeyboardInterrupt:
            elapsed = time.perf_counter() - started
            _print(f"    [中断] final trim 已运行 {elapsed:.1f}s，瓶颈位于 TokenBudget.trim_history()")
            raise
        finally:
            stop.set()
            heartbeat.join(timeout=0.2)
        elapsed = time.perf_counter() - started
        _print(f"    [阶段完成] final trim {elapsed:.2f}s | {len(messages)} -> {len(result)} messages")
        return result
    return _ORIGINAL_TRIM_HISTORY(self, messages, token_limit)


def _evaluate_with_progress(
    *,
    case: dict[str, Any],
    defaults: dict[str, Any],
    variant: str,
    workspace: Path,
    semantic_mode: str,
    live_backend,
) -> dict[str, Any]:
    _STATE["index"] = int(_STATE["index"]) + 1
    index = int(_STATE["index"])
    total = len(_PLAN)
    case_id = str(case["id"])
    _STATE["current"] = (case_id, variant)
    _STATE["trim_calls"] = 0
    started = time.perf_counter()
    _STATE["current_started"] = started

    completed = index - 1
    remaining = total - completed
    _print()
    _print(f"[{index}/{total}] 开始  case={case_id}  variant={variant}")
    _print(f"    已完成 {completed}/{total}，当前项之后还剩 {remaining - 1} 项")
    _print(f"    路径: {_stage_hint(variant)}")

    try:
        row = _ORIGINAL_EVALUATE(
            case=case,
            defaults=defaults,
            variant=variant,
            workspace=workspace,
            semantic_mode=semantic_mode,
            live_backend=live_backend,
        )
    except KeyboardInterrupt:
        elapsed = time.perf_counter() - started
        _print()
        _print(f"[中断] 第 {index}/{total} 项运行 {elapsed:.1f}s 后被中断")
        _print(f"    case={case_id}  variant={variant}")
        _print(f"    已完整完成 {completed}/{total} 项，尚有 {total - completed} 项未完成")
        raise
    finally:
        _STATE["current"] = None
        _STATE["current_started"] = None

    elapsed = time.perf_counter() - started
    status = "PASS" if row.get("passed") else "FAIL"
    failures = row.get("failures") or []
    _print(
        f"[{index}/{total}] 完成  {status}  {elapsed:.2f}s "
        f"| tokens {row.get('raw_history_tokens')} -> {row.get('policy_tokens')} -> {row.get('final_tokens')} "
        f"| summary_calls={row.get('summary_call_count', 0)}"
    )
    if failures:
        _print(f"    failures: {', '.join(str(item) for item in failures)}")
    _print(f"    总进度: {index}/{total} 完成，剩余 {total - index} 项")
    if index < total:
        next_case, next_variant = _PLAN[index]
        _print(f"    下一项: {next_case} / {next_variant}")
    else:
        _print("    下一步: 聚合 raw.jsonl / report.json / report.md / metadata.json")
    return row


def main() -> None:
    global _PLAN
    _extract_filter_args()
    _PLAN = _build_plan()
    _STATE["index"] = 0
    _print("Context Policy B1 benchmark（可观察模式）")
    _print(f"共 {len(_PLAN)} 个 case-variant replay；semantic mode/输出参数沿用原 benchmark CLI。")
    if _CASE_FILTER:
        _print(f"case 筛选: {', '.join(_CASE_FILTER)}")
    if _VARIANT_FILTER:
        _print(f"variant 筛选: {', '.join(_VARIANT_FILTER)}")
    _print("每项会显示当前阶段、耗时、关键 token 指标、已完成和下一项。")
    _print(f"final trim 超过 {_HEARTBEAT_SECONDS:.0f}s 时，每 {_HEARTBEAT_SECONDS:.0f}s 输出一次心跳。")
    benchmark.load_manifest = _filtered_manifest
    benchmark._evaluate_case_variant = _evaluate_with_progress
    TokenBudget.trim_history = _trim_history_with_progress
    try:
        benchmark.main()
    finally:
        benchmark.load_manifest = _ORIGINAL_LOAD_MANIFEST
        benchmark._evaluate_case_variant = _ORIGINAL_EVALUATE
        TokenBudget.trim_history = _ORIGINAL_TRIM_HISTORY


if __name__ == "__main__":
    main()
