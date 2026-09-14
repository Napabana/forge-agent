from __future__ import annotations

import json

from evals.harness import EvalResult, load_cases, materialize_case, summarize, verify_case
from evals.report import build_report


def test_fixed_cases_are_resettable_and_use_hidden_verifier(tmp_path):
    cases = load_cases()
    assert len(cases) == 6
    assert {case.category for case in cases} == {
        "single_file_bug", "cross_file_change", "failing_test_diagnosis",
        "hidden_test_feature", "refactor", "false_finish_guard",
    }
    case = cases[0]
    repo = materialize_case(case, tmp_path / "fixture")
    assert verify_case(case, repo).returncode != 0
    (repo / "calc.py").write_text("def divide(left, right):\n    return left / right\n")
    assert verify_case(case, repo).returncode == 0
    materialize_case(case, repo)
    assert "left * right" in (repo / "calc.py").read_text()


def test_ablation_summary_writes_json_and_markdown(tmp_path):
    results = [
        EvalResult("a", "static", True, False, 100, 0.1, 1.0, 2),
        EvalResult("b", "static", False, True, 50, 0.05, 3.0, 1, True),
    ]
    summary = summarize(results)
    assert summary["pass_at_1"] == 0.5
    assert summary["tokens_per_solved"] == 150
    assert summary["p95_latency_seconds"] == 3.0
    source = tmp_path / "runs.jsonl"
    source.write_text("\n".join(json.dumps(result.__dict__) for result in results))
    report = build_report(source, tmp_path / "summary")
    assert report["static"]["false_finish_rate"] == 0.5
    assert (tmp_path / "summary.json").exists()
    assert "| static |" in (tmp_path / "summary.md").read_text()
