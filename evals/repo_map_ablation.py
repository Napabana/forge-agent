"""Offline Repo Map benchmark derived from real git commit history.

For each selected commit, the commit subject is the query, the parent commit is
the repository snapshot, and ground truth is the set of pre-existing source
files actually changed by that commit. Added files are excluded because they do
not exist in the pre-change snapshot. No LLM calls are made.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import platform
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context import repo_map as repo_map_module  # noqa: E402
from context.repo_map import FileInfo, RepoMap  # noqa: E402
from context.token_budget import estimate_tokens  # noqa: E402

_MANIFEST = Path(__file__).parent / "fixtures" / "repo_map_ablation.json"
_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".cpp", ".c", ".rb",
})
_EXCLUDED_PARTS = frozenset({"test", "tests", "docs", "examples", "fixtures"})


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {error}")
    return result.stdout


def _single_parent(repo: Path, commit: str) -> str:
    fields = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    if len(fields) != 2:
        raise ValueError(f"benchmark commit must have exactly one parent: {commit}")
    return fields[1]


def _is_source(path: str) -> bool:
    p = PurePosixPath(path)
    if not p.parts or any(part.lower() in _EXCLUDED_PARTS for part in p.parts[:-1]):
        return False
    if p.name == "smoke_test.py" or p.name.startswith("test_") or p.name.endswith("_test.py"):
        return False
    return p.suffix.lower() in _SOURCE_SUFFIXES


def _ground_truth_files(repo: Path, parent: str, commit: str) -> tuple[str, ...]:
    """Pre-existing non-test source files changed by commit."""
    targets: set[str] = set()
    for line in _git(repo, "diff", "--name-status", "--find-renames", parent, commit).splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        code = fields[0][:1]
        if code == "A":
            continue
        if code in {"R", "C"} and len(fields) >= 3:
            candidate = fields[1]  # path that exists in the parent snapshot
        elif code in {"M", "D", "T"} and len(fields) >= 2:
            candidate = fields[1]
        else:
            continue
        if not _is_source(candidate):
            continue
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{parent}:{candidate}"],
            cwd=repo, capture_output=True, check=False,
        ).returncode == 0
        if exists:
            targets.add(candidate)
    return tuple(sorted(targets))


@contextmanager
def _snapshot(repo: Path, revision: str) -> Iterator[Path]:
    """Materialize a revision without altering/registering the user's worktree."""
    archive = _git_bytes(repo, "archive", "--format=tar", revision)
    with tempfile.TemporaryDirectory(prefix="forge-repomap-") as tmp:
        root = Path(tmp).resolve()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                target = (root / member.name).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"archive member escapes snapshot: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is not None:
                        target.write_bytes(source.read())
        yield root


def _rank(files: Sequence[FileInfo], query: str | None) -> list[FileInfo]:
    relevance = repo_map_module._query_relevance(list(files), query)
    return sorted(
        files,
        key=lambda item: (-(item.importance_score() + relevance.get(item.path, 0.0)), item.rel_path),
    )


def _visible(repo_map: RepoMap, ranked: Sequence[FileInfo], budget: int) -> tuple[str, ...]:
    used, limit = 0, budget * 4
    result: list[str] = []
    for item in ranked:
        block = repo_map._format_file(item)
        if used + len(block) > limit:
            break
        result.append(item.rel_path)
        used += len(block)
    return tuple(result)


def _ranking_metrics(ranked: Sequence[str], targets: Sequence[str], visible: Sequence[str]) -> dict:
    positions = {path: index + 1 for index, path in enumerate(ranked)}
    ranks = {target: positions.get(target) for target in targets}
    count = len(targets)
    finite = [rank for rank in ranks.values() if rank is not None]

    def recall(k: int) -> float:
        return sum(rank is not None and rank <= k for rank in ranks.values()) / count if count else 0.0

    visible_set = set(visible)
    return {
        "target_ranks": ranks,
        "recall_at_1": recall(1),
        "recall_at_3": recall(3),
        "recall_at_5": recall(5),
        "mrr": 1.0 / min(finite) if finite else 0.0,
        "mean_target_rank": statistics.mean(finite) if finite else None,
        "budget_target_recall": sum(t in visible_set for t in targets) / count if count else 0.0,
    }


def evaluate_retrieval_case(repo: Path, case: dict, budget: int) -> list[dict]:
    commit = case["commit"]
    parent = _single_parent(repo, commit)
    query = _git(repo, "show", "-s", "--format=%s", commit).strip()
    targets = _ground_truth_files(repo, parent, commit)
    if not targets:
        raise ValueError("no retrievable pre-change source targets")

    with _snapshot(repo, parent) as root:
        scanner = RepoMap(root)
        started = time.perf_counter()
        files, report = scanner._scan()
        scan_seconds = time.perf_counter() - started
        scanner._files, scanner._last_report = files, report
        rows = []
        for variant, variant_query in (("static", None), ("query_aware", query)):
            rank_started = time.perf_counter()
            ranked = _rank(files, variant_query)
            ranking_seconds = time.perf_counter() - rank_started
            visible = _visible(scanner, ranked, budget)
            summary = scanner.build(budget=budget, query=variant_query)
            rows.append({
                "case_id": case["id"], "commit": commit, "base_commit": parent,
                "query": query, "variant": variant, "target_files": list(targets),
                **_ranking_metrics([item.rel_path for item in ranked], targets, visible),
                "visible_files": len(visible), "files_ranked": len(ranked),
                "map_tokens": estimate_tokens(summary), "scan_seconds": scan_seconds,
                "ranking_seconds": ranking_seconds,
            })
        return rows


def _aggregate(rows: Sequence[dict]) -> dict[str, dict]:
    report: dict[str, dict] = {}
    for variant in ("static", "query_aware"):
        group = [row for row in rows if row["variant"] == variant]
        mean_ranks = [row["mean_target_rank"] for row in group if row["mean_target_rank"] is not None]
        report[variant] = {
            "cases": len(group),
            "recall_at_1": statistics.mean(row["recall_at_1"] for row in group) if group else 0.0,
            "recall_at_3": statistics.mean(row["recall_at_3"] for row in group) if group else 0.0,
            "recall_at_5": statistics.mean(row["recall_at_5"] for row in group) if group else 0.0,
            "mrr": statistics.mean(row["mrr"] for row in group) if group else 0.0,
            "mean_target_rank": statistics.mean(mean_ranks) if mean_ranks else 0.0,
            "budget_target_recall": statistics.mean(row["budget_target_recall"] for row in group) if group else 0.0,
            "mean_map_tokens": statistics.mean(row["map_tokens"] for row in group) if group else 0.0,
        }
    return report


def _historical_repo_map(repo: Path, revision: str, name: str):
    source = _git(repo, "show", f"{revision}:context/repo_map.py")
    module = types.ModuleType(name)
    module.__file__ = f"git:{revision}:context/repo_map.py"
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _semantic_hash(files: Sequence[object]) -> str:
    payload = [{
        "path": str(item.path), "imports": item.import_count, "references": item.reference_count,
        "symbols": [[s.name, s.kind, s.line, s.indent] for s in item.symbols],
    } for item in files]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0.0


def benchmark_reference_scoring(repo: Path, cfg: dict, repetitions: int | None = None) -> tuple[list[dict], dict]:
    baseline, optimized = cfg["baseline_commit"], cfg["optimized_commit"]
    snapshot_commit = cfg["snapshot_commit"]
    repeats = int(repetitions or cfg.get("repetitions", 5))
    samples: list[dict] = []
    with _snapshot(repo, snapshot_commit) as root:
        for repeat in range(1, repeats + 1):
            order = [("baseline", baseline), ("optimized", optimized)]
            if repeat % 2 == 0:
                order.reverse()
            for variant, revision in order:
                name = f"_forge_repo_map_{variant}_{repeat}_{time.time_ns()}"
                module = _historical_repo_map(repo, revision, name)
                try:
                    started = time.perf_counter()
                    files, scan_report = module.RepoMap(root)._scan()
                    samples.append({
                        "kind": "performance", "variant": variant,
                        "implementation_commit": revision, "snapshot_commit": snapshot_commit,
                        "repeat": repeat, "seconds": time.perf_counter() - started,
                        "files_discovered": scan_report.files_discovered,
                        "files_parsed": scan_report.files_parsed,
                        "semantic_hash": _semantic_hash(files),
                    })
                finally:
                    sys.modules.pop(name, None)

    summary: dict[str, object] = {}
    for variant in ("baseline", "optimized"):
        group = [sample for sample in samples if sample["variant"] == variant]
        times = [sample["seconds"] for sample in group]
        summary[variant] = {
            "implementation_commit": group[0]["implementation_commit"] if group else "",
            "runs": len(group), "median_seconds": statistics.median(times) if times else 0.0,
            "p95_seconds": _p95(times),
        }
    baseline_hashes = {x["semantic_hash"] for x in samples if x["variant"] == "baseline"}
    optimized_hashes = {x["semantic_hash"] for x in samples if x["variant"] == "optimized"}
    summary["semantic_equivalent"] = baseline_hashes == optimized_hashes and len(baseline_hashes) == 1
    b, o = summary["baseline"]["median_seconds"], summary["optimized"]["median_seconds"]
    summary["speedup"] = b / o if o else 0.0
    return samples, summary


def _write(output: Path, metadata: dict, retrieval: list[dict], skipped: list[dict], perf: list[dict], perf_summary: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "raw.jsonl").open("w", encoding="utf-8") as stream:
        for row in retrieval:
            stream.write(json.dumps({"kind": "retrieval", **row}, ensure_ascii=False) + "\n")
        for row in perf:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    aggregate = _aggregate(retrieval)
    report = {"metadata": metadata, "retrieval": aggregate, "skipped_cases": skipped, "performance": perf_summary}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Repo Map ablation", "",
        f"- Runner revision: `{metadata['runner_revision']}`",
        f"- Valid retrieval cases: `{len(retrieval) // 2}`", f"- Skipped cases: `{len(skipped)}`", "",
        "## Retrieval quality", "",
        "| variant | cases | R@1 | R@3 | R@5 | MRR | mean target rank | budget recall | mean map tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant, values in aggregate.items():
        lines.append(
            f"| {variant} | {values['cases']} | {values['recall_at_1']:.4f} | {values['recall_at_3']:.4f} | "
            f"{values['recall_at_5']:.4f} | {values['mrr']:.4f} | {values['mean_target_rank']:.3f} | "
            f"{values['budget_target_recall']:.4f} | {values['mean_map_tokens']:.1f} |"
        )
    lines += ["", "## Reference-scoring performance", ""]
    for variant in ("baseline", "optimized"):
        values = perf_summary[variant]
        lines.append(f"- {variant}: median `{values['median_seconds']:.4f}s`, p95 `{values['p95_seconds']:.4f}s`, runs `{values['runs']}`")
    lines += [
        f"- Speedup: `{perf_summary['speedup']:.3f}x`",
        f"- Semantic equivalent: `{perf_summary['semantic_equivalent']}`",
    ]
    if skipped:
        lines += ["", "## Skipped cases", ""] + [f"- `{x['case_id']}`: {x['reason']}" for x in skipped]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_benchmark(repo: str | Path, manifest: str | Path = _MANIFEST, output: str | Path = "evals/results/repo_map_ablation", *, allow_dirty: bool = False, performance_repetitions: int | None = None) -> dict:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repository root: {repo}")
    dirty = bool(_git(repo, "status", "--porcelain").strip())
    if dirty and not allow_dirty:
        raise RuntimeError("working tree is dirty; commit/stash changes or pass --allow-dirty")
    manifest_data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    retrieval, skipped = [], []
    budget = int(manifest_data.get("repo_map_budget_tokens", 10_200))
    for case in manifest_data["cases"]:
        try:
            retrieval.extend(evaluate_retrieval_case(repo, case, budget))
        except (RuntimeError, ValueError) as exc:
            skipped.append({"case_id": case["id"], "reason": str(exc)})
    perf, perf_summary = benchmark_reference_scoring(repo, manifest_data["performance"], performance_repetitions)
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner_revision": _git(repo, "rev-parse", "HEAD").strip(),
        "working_tree_dirty": dirty, "python": sys.version.split()[0], "platform": platform.platform(),
    }
    return _write(Path(output), metadata, retrieval, skipped, perf, perf_summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", default=str(_MANIFEST))
    parser.add_argument("--output", default="evals/results/repo_map_ablation")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--performance-repetitions", type=int)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(
        args.repo, args.manifest, args.output,
        allow_dirty=args.allow_dirty, performance_repetitions=args.performance_repetitions,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
