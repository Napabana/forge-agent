from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evals.repo_map_ablation import (
    _ground_truth_files,
    _ranking_metrics,
    _single_parent,
    evaluate_retrieval_case,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "bench@example.com")
    _git(repo, "config", "user.name", "Benchmark")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_ground_truth_uses_only_preexisting_non_test_source_files(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "payments.py").write_text("class RefundLedger:\n    pass\n", encoding="utf-8")
    (repo / "tests" / "test_payments.py").write_text("def test_old(): pass\n", encoding="utf-8")
    base = _commit(repo, "base")

    (repo / "src" / "payments.py").write_text("class RefundLedger:\n    enabled = True\n", encoding="utf-8")
    (repo / "src" / "new_feature.py").write_text("def new_feature(): pass\n", encoding="utf-8")
    (repo / "tests" / "test_payments.py").write_text("def test_new(): pass\n", encoding="utf-8")
    changed = _commit(repo, "fix RefundLedger in payments")

    assert _single_parent(repo, changed) == base
    assert _ground_truth_files(repo, base, changed) == ("src/payments.py",)


def test_ranking_metrics_count_missing_targets_in_denominator():
    metrics = _ranking_metrics(
        ["b.py", "a.py", "other.py"],
        ["a.py", "missing.py"],
        ["b.py", "a.py"],
    )

    assert metrics["recall_at_1"] == 0.0
    assert metrics["recall_at_3"] == 0.5
    assert metrics["recall_at_5"] == 0.5
    assert metrics["mrr"] == pytest.approx(0.5)
    assert metrics["mean_target_rank"] == 2
    assert metrics["budget_target_recall"] == 0.5


def test_query_aware_ranking_improves_symbol_matched_history_case(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "generic.py").write_text("class CommonService:\n    pass\n", encoding="utf-8")
    (repo / "payments.py").write_text("class RefundLedger:\n    pass\n", encoding="utf-8")
    _commit(repo, "base")

    (repo / "payments.py").write_text("class RefundLedger:\n    enabled = True\n", encoding="utf-8")
    changed = _commit(repo, "fix RefundLedger in payments")

    results = evaluate_retrieval_case(
        repo,
        {"id": "refund-ledger", "commit": changed},
        budget=2_000,
    )
    by_variant = {result["variant"]: result for result in results}

    assert by_variant["query_aware"]["mrr"] >= by_variant["static"]["mrr"]
    assert by_variant["query_aware"]["target_ranks"]["payments.py"] == 1
    assert by_variant["query_aware"]["recall_at_1"] == 1.0


def test_query_aware_ranking_improves_content_only_history_case(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "runtime.py").write_text(
        "def execute():\n"
        "    # provider retry timeout failure recovery\n"
        "    return None\n",
        encoding="utf-8",
    )
    (repo / "core.py").write_text(
        "\n".join(f"class Service{i}: pass" for i in range(12)) + "\n",
        encoding="utf-8",
    )
    _commit(repo, "base")

    (repo / "runtime.py").write_text(
        "def execute():\n"
        "    # provider retry timeout failure recovery\n"
        "    return False\n",
        encoding="utf-8",
    )
    changed = _commit(repo, "handle provider retry timeout failure")

    results = evaluate_retrieval_case(
        repo,
        {"id": "provider-retry", "commit": changed},
        budget=2_000,
    )
    by_variant = {result["variant"]: result for result in results}

    assert by_variant["query_aware"]["target_ranks"]["runtime.py"] == 1
    assert by_variant["query_aware"]["mrr"] > by_variant["static"]["mrr"]
