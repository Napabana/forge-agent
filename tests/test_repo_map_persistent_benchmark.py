from __future__ import annotations

import subprocess

from evals.repo_map_persistent_benchmark import benchmark_runtime, _stats


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_runtime_benchmark_separates_cold_warm_and_incremental_phases(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "benchmark@test.invalid")
    _git(repo, "config", "user.name", "Benchmark")
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    (repo / "b.py").write_text("def beta():\n    return alpha()\n")
    (repo / "c.py").write_text("from a import alpha\n\ndef gamma():\n    return alpha()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")

    rows, report = benchmark_runtime(
        repo,
        repetitions=1,
        mutation_paths=("a.py", "b.py"),
        query="alpha gamma",
    )

    assert len(rows) == 1
    assert report["semantic_equivalent"] is True
    assert report["warm_start_reparsed_zero_files"] is True
    assert report["single_file_update_parsed_only_one"] is True
    assert report["multi_file_update_parsed_only_two"] is True
    assert report["working_tree_clean_after"] is True
    assert rows[0]["warm_files_parsed"] == 0
    assert rows[0]["single_files_parsed"] == 1
    assert rows[0]["multi_files_parsed"] == 2


def test_stats_reports_median_and_p95():
    result = _stats([0.1, 0.2, 0.3])
    assert result["runs"] == 3
    assert result["median_seconds"] == 0.2
    assert result["p95_seconds"] == 0.3
