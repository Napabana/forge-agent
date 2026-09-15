"""Context Policy benchmark 的可观察 CLI；不改变 benchmark 计算逻辑。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from context.token_budget import TokenBudget
from evals import context_policy_benchmark as benchmark


_ORIGINAL_EVALUATE = benchmark._evaluate_case_variant
_ORIGINAL_TRIM_HISTORY = TokenBudget.trim_history


def _manifest_path_from_argv() -> Path:
    """只读取 CLI 中的 --manifest；其余参数继续交给原 benchmark.main()。"""
    for index, arg in enumerate(sys.argv[:-1]):
        if arg == "--manifest":
            return Path(sys.argv[index + 1]).resolve()
    return benchmark._MANIFEST


def _build_plan() -> list[tuple[str, str]]:
    data = benchmark.load_manifest(_manifest_path_from_argv())
    plan: list[tuple[str, str]] = []
    for case in data["cases"]:
        for variant in case.get("applicable_variants", benchmark._VARIANTS):
            plan.append((str(case["id"]), str(variant)))
    return plan


_PLAN = _build_plan()
_STATE: dict[str, Any] = {
    "index": 0,
    "current": None,
    "current_started": None,
    "trim_calls": 0,
}


def _print(message: str = "") -> None:
    print(message, flush=True)


def _stage_hint(variant: str) -> str:
    if variant == "budget_trim_only":
        return "物化 History -> baseline view -> final TokenBudget trim -> metrics"
    if variant == "deterministic_pruning":
        return "物化 History -> Stage A deterministic pruning -> final TokenBudget trim -> metrics"
    return "物化 History -> Hybrid Stage A/Stage B -> final TokenBudget trim -> metrics"


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
        try:
            result = _ORIGINAL_TRIM_HISTORY(self, messages, token_limit)
        except KeyboardInterrupt:
            elapsed = time.perf_counter() - started
            _print(f"    [中断] final trim 已运行 {elapsed:.1f}s，瓶颈位于 TokenBudget.trim_history()")
            raise
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
    _print("Context Policy B1 benchmark（可观察模式）")
    _print(f"共 {len(_PLAN)} 个 case-variant replay；semantic mode/输出参数沿用原 benchmark CLI。")
    _print("每项会显示当前阶段、耗时、关键 token 指标、已完成和下一项。")
    benchmark._evaluate_case_variant = _evaluate_with_progress
    TokenBudget.trim_history = _trim_history_with_progress
    try:
        benchmark.main()
    finally:
        benchmark._evaluate_case_variant = _ORIGINAL_EVALUATE
        TokenBudget.trim_history = _ORIGINAL_TRIM_HISTORY


if __name__ == "__main__":
    main()
