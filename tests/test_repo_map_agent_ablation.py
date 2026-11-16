from __future__ import annotations

import json

from agent.task import EventType
from evals.repo_map_agent_ablation import (
    _aggregate,
    load_manifest,
    trace_exploration_metrics,
    validate_harness,
)


def test_repo_map_agent_manifest_has_frozen_four_variant_protocol():
    defaults, cases = load_manifest()
    assert defaults["max_steps"] > 0
    assert defaults["budget_tokens"] > 0
    assert len(cases) >= 4
    assert all(case.target_files for case in cases)
    assert all(case.verifier for case in cases)


def test_trace_exploration_metrics_counts_target_read_and_search(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_text("value = 1\n")
    trace = tmp_path / "trace.jsonl"
    events = [
        {
            "event_type": EventType.ACTION.value,
            "payload": {
                "step": 1,
                "action": {"tool_call": {"name": "find_files", "params": {"pattern": "*.py"}}},
            },
        },
        {
            "event_type": EventType.ACTION.value,
            "payload": {
                "step": 2,
                "action": {"tool_call": {"name": "search_text", "params": {"pattern": "value"}}},
            },
        },
        {
            "event_type": EventType.ACTION.value,
            "payload": {
                "step": 3,
                "action": {"tool_call": {"name": "file_read", "params": {"path": "target.py"}}},
            },
        },
    ]
    trace.write_text("\n".join(json.dumps(item) for item in events) + "\n")
    metrics = trace_exploration_metrics(str(trace), repo, ("target.py",))
    assert metrics["first_target_read_step"] == 3
    assert metrics["files_read"] == 1
    assert metrics["find_files_calls"] == 1
    assert metrics["search_text_calls"] == 1
    assert metrics["tool_calls"] == 3


def test_aggregate_uses_observed_small_sample_counts_not_pass_at_1():
    rows = [
        {
            "variant": "no_repo_map",
            "solved": True,
            "verifier_passed": True,
            "false_finish": False,
            "first_target_read_step": 2,
            "files_read": 2,
            "search_text_calls": 1,
            "find_files_calls": 1,
            "input_tokens": 100,
            "total_tokens": 120,
            "cached_input_tokens": 20,
            "latency_seconds": 1.0,
            "repo_map_build_seconds": 0.0,
            "repo_map_incremental_update_seconds": 0.0,
        },
    ]
    report = _aggregate(rows, ["no_repo_map"])
    values = report["variants"]["no_repo_map"]
    assert values["observed_solved"] == 1
    assert "pass_at_1" not in values


def test_validate_only_writes_explicit_not_executed_evidence(tmp_path):
    output = tmp_path / "results"
    result = validate_harness(output=output, reason="test_no_provider")
    assert result["rows"] == 0
    metadata = json.loads((output / "metadata.json").read_text())
    report = json.loads((output / "report.json").read_text())
    assert metadata["real_model_executed"] is False
    assert metadata["execution_status"] == "not_executed"
    assert metadata["not_executed_reason"] == "test_no_provider"
    assert report["rows"] == 0
    assert (output / "raw.jsonl").read_text() == ""
