"""Persistent/incremental Repo Map semantic and performance benchmark.

This benchmark has two independent parts:
1. Re-run the frozen 12-case commit-history retrieval protocol and require the
   persistent implementation to be semantically/ranking equivalent to the
   current legacy Query-aware RepoMap.
2. Measure cold build, warm start, query-only rerank, single-file update,
   multi-file update, and explicit full rebuild separately on one repository.

No LLM calls are made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from context.incremental_repo_map import PersistentRepoMap
from context.repo_map import RepoMap
from evals.repo_map_ablation import (
    _aggregate,
    _git,
    _ground_truth_files,
    _rank,
    _ranking_metrics,
    _semantic_hash,
    _single_parent,
    _snapshot,
    _visible,
)


_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = Path(__file__).parent / "fixtures" / "repo_map_ablation.json"
_FROZEN_REPORT = Path(__file__).parent / "results" / "repo_map_ablation" / "report.json"
_DEFAULT_MUTATION_PATHS = ("context/repo_map.py", "context/repository_state.py")


def evaluate_persistent_retrieval(
    repo: Path,
    manifest_data: dict[str, Any],
    *,
    strict: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    budget = int(manifest_data.get("repo_map_budget_tokens", 10_200))
    rows: list[dict[str, Any]] = []
    for case in manifest_data["cases"]:
        commit = case["commit"]
        parent = _single_parent(repo, commit)
        query = _git(repo, "show", "-s", "--format=%s", commit).strip()
        targets = _ground_truth_files(repo, parent, commit)
        if not targets:
            raise ValueError(f"{case['id']}: no retrievable pre-change source targets")
        with _snapshot(repo, parent) as root, tempfile.TemporaryDirectory(
            prefix="forge-repomap-index-"
        ) as tmp:
            legacy = RepoMap(root)
            legacy_files, legacy_report = legacy._scan()
            legacy._files, legacy._last_report = legacy_files, legacy_report
            legacy_ranked = _rank(legacy_files, query)
            legacy_visible = _visible(legacy, legacy_ranked, budget)
            legacy_summary = legacy.build(budget=budget, query=query)

            persistent = PersistentRepoMap(
                root,
                index_path=Path(tmp) / "index.sqlite3",
            )
            persistent_summary = persistent.build(budget=budget, query=query)
            persistent_files = list(persistent._files or ())
            persistent_ranked = _rank(persistent_files, query)
            persistent_visible = _visible(persistent, persistent_ranked, budget)

            legacy_paths = [item.rel_path for item in legacy_ranked]
            persistent_paths = [item.rel_path for item in persistent_ranked]
            semantic_equal = _semantic_hash(legacy_files) == _semantic_hash(persistent_files)
            ranking_equal = legacy_paths == persistent_paths
            visible_equal = tuple(legacy_visible) == tuple(persistent_visible)
            rendering_equal = legacy_summary == persistent_summary
            equivalent = semantic_equal and ranking_equal and visible_equal and rendering_equal
            row = {
                "case_id": case["id"],
                "commit": commit,
                "base_commit": parent,
                "query": query,
                "target_files": list(targets),
                "semantic_equivalent": semantic_equal,
                "ranking_equivalent": ranking_equal,
                "visible_equivalent": visible_equal,
                "rendering_equivalent": rendering_equal,
                "equivalent": equivalent,
                **_ranking_metrics(persistent_paths, targets, persistent_visible),
            }
            rows.append(row)
            persistent._index.close()
            if strict and not equivalent:
                raise AssertionError(f"{case['id']}: persistent Repo Map changed retrieval semantics")

    persistent_rows = [
        {
            "variant": "incremental_query_aware",
            **row,
        }
        for row in rows
    ]
    aggregate = _aggregate([
        {
            "variant": "query_aware",
            **row,
            "map_tokens": 0,
        }
        for row in rows
    ])
    persistent_metrics = dict(aggregate["query_aware"])
    comparison = {
        "case_count": len(rows),
        "all_semantic_equivalent": all(row["semantic_equivalent"] for row in rows),
        "all_ranking_equivalent": all(row["ranking_equivalent"] for row in rows),
        "all_visible_equivalent": all(row["visible_equivalent"] for row in rows),
        "all_rendering_equivalent": all(row["rendering_equivalent"] for row in rows),
        "all_equivalent": all(row["equivalent"] for row in rows),
        "incremental_query_aware": persistent_metrics,
    }
    return persistent_rows, comparison


def compare_with_frozen_report(
    persistent_summary: dict[str, Any],
    frozen_report_path: str | Path = _FROZEN_REPORT,
    *,
    tolerance: float = 1e-12,
    strict: bool = False,
) -> dict[str, Any]:
    frozen = json.loads(Path(frozen_report_path).read_text(encoding="utf-8"))
    expected = frozen["retrieval"]["query_aware"]
    actual = persistent_summary["incremental_query_aware"]
    fields = (
        "mrr",
        "budget_target_recall",
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mean_target_rank",
    )
    deltas = {field: float(actual[field]) - float(expected[field]) for field in fields}
    equivalent = all(abs(delta) <= tolerance for delta in deltas.values())
    result = {
        "frozen_query_aware": {field: expected[field] for field in fields},
        "incremental_query_aware": {field: actual[field] for field in fields},
        "deltas": deltas,
        "tolerance": tolerance,
        "equivalent": equivalent,
    }
    if strict and not equivalent:
        raise AssertionError(f"persistent retrieval differs from frozen report: {deltas}")
    return result


def benchmark_runtime(
    repo: Path,
    *,
    repetitions: int = 5,
    mutation_paths: Sequence[str] = _DEFAULT_MUTATION_PATHS,
    query: str = "persistent repo map query ranking reference graph",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if len(mutation_paths) < 2:
        raise ValueError("at least two mutation paths are required")
    paths = tuple(Path(item) for item in mutation_paths)
    originals: dict[Path, bytes] = {}
    for relative in paths:
        absolute = repo / relative
        if not absolute.is_file():
            raise ValueError(f"benchmark mutation path is not a file: {relative}")
        originals[relative] = absolute.read_bytes()

    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="forge-repomap-runtime-") as cache_tmp:
        cache_root = Path(cache_tmp)
        for repeat in range(1, repetitions + 1):
            index_path = cache_root / f"repeat-{repeat}.sqlite3"
            legacy = RepoMap(repo)
            legacy_started = time.perf_counter()
            legacy.build(query=query)
            legacy_seconds = time.perf_counter() - legacy_started
            legacy_hash = _semantic_hash(list(legacy._files or ()))

            persistent: PersistentRepoMap | None = None
            warm: PersistentRepoMap | None = None
            try:
                cold_started = time.perf_counter()
                persistent = PersistentRepoMap(repo, index_path=index_path)
                persistent.build(query=query)
                cold_seconds = time.perf_counter() - cold_started
                cold_hash = _semantic_hash(list(persistent._files or ()))
                cold_parsed = persistent.last_report.files_parsed if persistent.last_report else -1
                persistent._index.close()

                warm_started = time.perf_counter()
                warm = PersistentRepoMap(repo, index_path=index_path)
                warm.build(query=query)
                warm_seconds = time.perf_counter() - warm_started
                warm_parsed = warm.last_report.files_parsed if warm.last_report else -1

                rerank_started = time.perf_counter()
                warm.build(query="repository state changed files incremental cache")
                rerank_seconds = time.perf_counter() - rerank_started
                rerank_operation = warm.metrics.last_operation

                single_rel = paths[0]
                _mutate_file(repo / single_rel, originals[single_rel], repeat, "single")
                single_started = time.perf_counter()
                single_report = warm.sync()
                single_seconds = time.perf_counter() - single_started

                (repo / single_rel).write_bytes(originals[single_rel])
                warm.sync()

                for relative in paths[:2]:
                    _mutate_file(repo / relative, originals[relative], repeat, "multi")
                multi_started = time.perf_counter()
                multi_report = warm.sync()
                multi_seconds = time.perf_counter() - multi_started

                for relative in paths[:2]:
                    (repo / relative).write_bytes(originals[relative])
                warm.sync()

                full_started = time.perf_counter()
                full_report = warm.refresh()
                full_seconds = time.perf_counter() - full_started
                final_hash = _semantic_hash(list(warm._files or ()))

                samples.append({
                    "repeat": repeat,
                    "legacy_build_seconds": legacy_seconds,
                    "cold_build_seconds": cold_seconds,
                    "warm_load_seconds": warm_seconds,
                    "query_rerank_seconds": rerank_seconds,
                    "single_file_update_seconds": single_seconds,
                    "multi_file_update_seconds": multi_seconds,
                    "full_rebuild_seconds": full_seconds,
                    "legacy_semantic_hash": legacy_hash,
                    "cold_semantic_hash": cold_hash,
                    "final_semantic_hash": final_hash,
                    "semantic_equivalent": legacy_hash == cold_hash == final_hash,
                    "cold_files_parsed": cold_parsed,
                    "warm_files_parsed": warm_parsed,
                    "single_files_parsed": single_report.files_parsed,
                    "multi_files_parsed": multi_report.files_parsed,
                    "full_rebuild_files_parsed": full_report.files_parsed,
                    "query_rerank_operation": rerank_operation,
                })
            finally:
                for relative, content in originals.items():
                    (repo / relative).write_bytes(content)
                if warm is not None:
                    warm._index.close()
                elif persistent is not None:
                    persistent._index.close()

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    clean_after = status.returncode == 0 and not status.stdout.strip()
    metrics = (
        "legacy_build_seconds",
        "cold_build_seconds",
        "warm_load_seconds",
        "query_rerank_seconds",
        "single_file_update_seconds",
        "multi_file_update_seconds",
        "full_rebuild_seconds",
    )
    summary = {
        key: _stats([float(row[key]) for row in samples])
        for key in metrics
    }
    summary.update({
        "runs": len(samples),
        "semantic_equivalent": all(bool(row["semantic_equivalent"]) for row in samples),
        "warm_start_reparsed_zero_files": all(int(row["warm_files_parsed"]) == 0 for row in samples),
        "single_file_update_parsed_only_one": all(
            int(row["single_files_parsed"]) == 1 for row in samples
        ),
        "multi_file_update_parsed_only_two": all(
            int(row["multi_files_parsed"]) == 2 for row in samples
        ),
        "working_tree_clean_after": clean_after,
        "mutation_paths": [item.as_posix() for item in paths[:2]],
    })
    return samples, summary


def _mutate_file(path: Path, original: bytes, repeat: int, label: str) -> None:
    suffix = f"\n# forge-repomap-benchmark-{label}-{repeat}\n".encode()
    path.write_bytes(original + suffix)


def _stats(values: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1) if ordered else 0
    return {
        "runs": len(values),
        "median_seconds": statistics.median(values) if values else 0.0,
        "p95_seconds": ordered[p95_index] if ordered else 0.0,
    }


def run_benchmark(
    repo: str | Path,
    *,
    manifest: str | Path = _MANIFEST,
    frozen_report: str | Path = _FROZEN_REPORT,
    output: str | Path = "evals/results/repo_map_persistent_benchmark",
    repetitions: int = 5,
    strict: bool = False,
) -> dict[str, Any]:
    repo = Path(repo).resolve()
    manifest_path = Path(manifest).resolve()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repository root: {repo}")
    dirty_before = _git(repo, "status", "--porcelain").strip()
    if dirty_before:
        raise RuntimeError("working tree must be clean before persistent Repo Map benchmark")

    retrieval_rows, retrieval = evaluate_persistent_retrieval(
        repo,
        manifest_data,
        strict=strict,
    )
    frozen_comparison = compare_with_frozen_report(
        retrieval,
        frozen_report,
        strict=strict,
    )
    performance_rows, performance = benchmark_runtime(
        repo,
        repetitions=repetitions,
    )
    if strict:
        checks = (
            retrieval["all_equivalent"],
            frozen_comparison["equivalent"],
            performance["semantic_equivalent"],
            performance["warm_start_reparsed_zero_files"],
            performance["single_file_update_parsed_only_one"],
            performance["multi_file_update_parsed_only_two"],
            performance["working_tree_clean_after"],
        )
        if not all(checks):
            raise AssertionError("persistent Repo Map strict benchmark invariant failed")

    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner_revision": _git(repo, "rev-parse", "HEAD").strip(),
        "fixture_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "retrieval_case_count": len(retrieval_rows),
        "performance_runs": repetitions,
        "strict": strict,
    }
    report = {
        "metadata": metadata,
        "retrieval": retrieval,
        "frozen_comparison": frozen_comparison,
        "performance": performance,
        "claim_boundary": (
            "Retrieval metrics are the frozen commit-history protocol. Runtime "
            "numbers separate cold build, warm load, incremental update, and full "
            "rebuild; none is an end-to-end Agent latency claim."
        ),
    }
    _write_outputs(Path(output), metadata, report, retrieval_rows, performance_rows)
    return report


def _write_outputs(
    output_dir: Path,
    metadata: dict[str, Any],
    report: dict[str, Any],
    retrieval_rows: list[dict[str, Any]],
    performance_rows: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "raw.jsonl").open("w", encoding="utf-8") as stream:
        for row in retrieval_rows:
            stream.write(json.dumps({"kind": "retrieval", **row}, ensure_ascii=False) + "\n")
        for row in performance_rows:
            stream.write(json.dumps({"kind": "performance", **row}, ensure_ascii=False) + "\n")
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    p = report["performance"]
    r = report["retrieval"]["incremental_query_aware"]
    lines = [
        "# Persistent / Incremental Repo Map Benchmark",
        "",
        f"- Runner revision: `{metadata['runner_revision']}`",
        f"- Retrieval cases: `{metadata['retrieval_case_count']}`",
        f"- Performance runs: `{metadata['performance_runs']}`",
        "",
        "## Frozen retrieval protocol",
        "",
        f"- Semantic/ranking/rendering equivalent on all cases: `{report['retrieval']['all_equivalent']}`",
        f"- MRR: `{r['mrr']:.6f}`",
        f"- Budget target recall: `{r['budget_target_recall']:.6f}`",
        f"- Matches frozen Query-aware report: `{report['frozen_comparison']['equivalent']}`",
        "",
        "## Runtime phases",
        "",
        "| phase | median (s) | p95 (s) |",
        "| --- | ---: | ---: |",
    ]
    for key in (
        "legacy_build_seconds",
        "cold_build_seconds",
        "warm_load_seconds",
        "query_rerank_seconds",
        "single_file_update_seconds",
        "multi_file_update_seconds",
        "full_rebuild_seconds",
    ):
        values = p[key]
        lines.append(
            f"| {key} | {values['median_seconds']:.6f} | {values['p95_seconds']:.6f} |"
        )
    lines += [
        "",
        f"- Warm start reparsed zero files: `{p['warm_start_reparsed_zero_files']}`",
        f"- Single-file update parsed only one file: `{p['single_file_update_parsed_only_one']}`",
        f"- Multi-file update parsed only two files: `{p['multi_file_update_parsed_only_two']}`",
        f"- Semantic equivalent: `{p['semantic_equivalent']}`",
        f"- Working tree clean after benchmark: `{p['working_tree_clean_after']}`",
        "",
        "These timings are Repo Map phases on the recorded machine, not end-to-end Agent latency.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", default=str(_MANIFEST))
    parser.add_argument("--frozen-report", default=str(_FROZEN_REPORT))
    parser.add_argument("--output", default="evals/results/repo_map_persistent_benchmark")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    result = run_benchmark(
        args.repo,
        manifest=args.manifest,
        frozen_report=args.frozen_report,
        output=args.output,
        repetitions=args.repetitions,
        strict=args.strict,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
