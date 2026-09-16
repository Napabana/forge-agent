"""P1-6 Evidence Pack 的只读离线一致性校验。

该入口只读取仓库内源码、测试与冻结结果，不调用 Provider/GitHub，
不重跑 benchmark，不修改 fixture、evals/results 或仓库状态。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

B1_VARIANTS = (
    "budget_trim_only",
    "deterministic_pruning",
    "hybrid_compaction",
)
B2_VARIANTS = ("baseline", "pruning_only", "hybrid_compaction")
B2_CASES = ("long-hard-constraint", "huge-tool-history", "superseded-state")

REQUIRED_PATHS = (
    "docs/evidence/README.md",
    "agent/core.py",
    "agent/runner.py",
    "agent/event_log.py",
    "agent/trace_v2.py",
    "harness/executor.py",
    "harness/hooks.py",
    "harness/permission.py",
    "context/compaction.py",
    "context/repo_map.py",
    "agent/session_store.py",
    "runtime/worktree.py",
    "tools/runtime.py",
    "entry/github_issue.py",
    "llm/base.py",
    "llm/router.py",
    "tests/test_failure_harness.py",
    "tests/test_tool_lifecycle_p0_2.py",
    "tests/test_trace_v2.py",
    "tests/test_agent_completion_guards.py",
    "tests/test_runner.py",
    "tests/test_session_store.py",
    "tests/test_github_issue_delivery.py",
    "tests/test_context_policy_benchmark.py",
    "tests/test_repo_map_ablation.py",
    "evals/context_policy_benchmark.py",
    "evals/repo_map_ablation.py",
    "evals/pr_test_issue_4_verifier.py",
    "evals/results/context_policy_benchmark/metadata.json",
    "evals/results/context_policy_benchmark/report.json",
    "evals/results/context_policy_agent_ablation_v3/metadata.json",
    "evals/results/context_policy_agent_ablation_v3/report.json",
    "evals/results/repo_map_ablation/report.json",
    "docs/changes/2026-09-15/pr-test真实PR改动内容.md",
)


def _load_json(root: Path, relative: str, errors: list[str]) -> dict[str, Any]:
    path = root / relative
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 校验器需要统一汇总可读错误
        errors.append(f"{relative}: cannot read JSON: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{relative}: top-level JSON must be an object")
        return {}
    return value


def _expect(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _close(actual: Any, expected: float, *, rel_tol: float = 1e-9) -> bool:
    try:
        return math.isclose(float(actual), expected, rel_tol=rel_tol, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def verify(root: Path | None = None) -> list[str]:
    """返回 Evidence Pack 一致性错误；空列表表示校验通过。"""
    repo = (root or ROOT).resolve()
    errors: list[str] = []

    for relative in REQUIRED_PATHS:
        _expect(errors, (repo / relative).is_file(), f"missing evidence file: {relative}")

    index_path = repo / "docs/evidence/README.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""

    b1_meta = _load_json(
        repo, "evals/results/context_policy_benchmark/metadata.json", errors
    )
    b1_report = _load_json(
        repo, "evals/results/context_policy_benchmark/report.json", errors
    )
    _expect(errors, b1_meta.get("schema_version") == 1, "B1 schema_version must be 1")
    _expect(errors, b1_meta.get("semantic", {}).get("mode") == "fixture", "B1 semantic mode must be fixture")
    _expect(errors, b1_meta.get("case_count") == 7, "B1 case_count must be 7")
    _expect(errors, tuple(b1_meta.get("variants", ())) == B1_VARIANTS, "B1 variants changed")
    _expect(errors, b1_report.get("rows") == 21, "B1 row count must be 21")
    b1_agg = b1_report.get("aggregate", {})
    _expect(errors, tuple(b1_agg.keys()) == B1_VARIANTS, "B1 aggregate variants changed")
    for variant in B1_VARIANTS:
        _expect(errors, b1_agg.get(variant, {}).get("runs") == 7, f"B1 {variant} runs must be 7")
    hybrid = b1_agg.get("hybrid_compaction", {})
    _expect(errors, hybrid.get("passed") == 7, "B1 hybrid passed must be 7")
    _expect(errors, _close(hybrid.get("pass_rate"), 1.0), "B1 hybrid pass_rate must be 1.0")
    _expect(errors, _close(hybrid.get("mean_hard_constraint_recall"), 1.0), "B1 hybrid hard-constraint recall must be 1.0")
    _expect(errors, _close(hybrid.get("mean_recent_raw_recall"), 1.0), "B1 hybrid recent-raw recall must be 1.0")
    _expect(errors, hybrid.get("summary_total_tokens") == 0, "B1 fixture summary tokens must remain 0")

    b2_meta = _load_json(
        repo, "evals/results/context_policy_agent_ablation_v3/metadata.json", errors
    )
    b2_report = _load_json(
        repo, "evals/results/context_policy_agent_ablation_v3/report.json", errors
    )
    _expect(errors, b2_meta.get("schema_version") == 1, "B2 schema_version must be 1")
    _expect(errors, b2_meta.get("model") == "deepseek-v4.1-flash", "B2 model changed")
    _expect(errors, b2_meta.get("provider") == "openai", "B2 provider changed")
    _expect(errors, tuple(b2_meta.get("cases", ())) == B2_CASES, "B2 cases changed")
    _expect(errors, tuple(b2_meta.get("variants", ())) == B2_VARIANTS, "B2 variants changed")
    _expect(errors, b2_meta.get("run_count") == 9, "B2 run_count must be 9")
    _expect(errors, all(b2_report.get(v, {}).get("runs") == 3 for v in B2_VARIANTS), "B2 each variant must contain exactly 3 runs")
    _expect(errors, [b2_report.get(v, {}).get("solved") for v in B2_VARIANTS] == [1, 2, 2], "B2 solved counts changed")

    repo_map = _load_json(repo, "evals/results/repo_map_ablation/report.json", errors)
    retrieval = repo_map.get("retrieval", {})
    static = retrieval.get("static", {})
    query = retrieval.get("query_aware", {})
    perf = repo_map.get("performance", {})
    _expect(errors, static.get("cases") == 12 and query.get("cases") == 12, "Repo Map case count must be 12 for both variants")
    _expect(errors, repo_map.get("skipped_cases") == [], "Repo Map formal report must have no skipped cases")
    _expect(errors, _close(static.get("mrr"), 0.0969540782040782), "Repo Map static MRR changed")
    _expect(errors, _close(query.get("mrr"), 0.31875), "Repo Map query-aware MRR changed")
    _expect(errors, _close(static.get("budget_target_recall"), 0.36491402116402116), "Repo Map static budget recall changed")
    _expect(errors, _close(query.get("budget_target_recall"), 0.6352513227513228), "Repo Map query-aware budget recall changed")
    _expect(errors, perf.get("baseline", {}).get("runs") == 5 and perf.get("optimized", {}).get("runs") == 5, "Repo Map reference-count timing must keep 5 runs per implementation")
    _expect(errors, perf.get("semantic_equivalent") is True, "Repo Map reference-count implementations must remain semantically equivalent")
    _expect(errors, _close(perf.get("speedup"), 71.25522946975629), "Repo Map reference-count speedup changed")

    anchors = (
        "B1: 7 cases × 3 variants = 21 rows",
        "Repo Map MRR: 0.096954 → 0.318750",
        "Repo Map budget target recall: 0.364914 → 0.635251",
        "Repo Map reference-count median: 35.1176s → 0.4928s (71.26×)",
        "B2 v3: 3 cases × 3 variants × 1 run = 9 real-model runs",
        "Real GitHub delivery cases: 1",
        "INSUFFICIENT EVIDENCE",
    )
    for anchor in anchors:
        _expect(errors, anchor in index_text, f"Evidence Index missing machine-checkable anchor: {anchor}")

    return errors


def main() -> int:
    errors = verify()
    if errors:
        print("Evidence Pack verification FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Evidence Pack verification passed.")
    print("B1=7x3 frozen replay; B2-v3=9 real-model small-sample runs; RepoMap=12 frozen cases; real GitHub delivery cases=1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
