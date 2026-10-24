"""Abstention pilot 的确定性协议测试；不调用真实 Provider。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.coding_agent.abstention_pilot import build_pilot, score_paired
from evals.coding_agent.schema import EvaluationSuite


ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "evals/fixtures/coding_agent/benchmark_v1.json"


def test_build_pilot_uses_frozen_source_and_isolates_task_pairs():
    original = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    suite, manifest = build_pilot(original)
    EvaluationSuite.from_dict(suite)
    assert suite["source"] == original["source"]
    assert len(manifest["pairs"]) == 4
    assert len(suite["tasks"]) == 8
    assert {pair["scenario"] for pair in manifest["pairs"]} == {"s3-conflicting-constraints"}
    index = {task["id"]: task for task in suite["tasks"]}
    for pair in manifest["pairs"]:
        act = index[pair["act_task_id"]]
        abstain = index[pair["abstain_task_id"]]
        assert act["files"] == abstain["files"]  # 相同初态
        assert abstain["reference_files"] == abstain["files"]  # 参考解不得修改
        assert act["require_changes"] is True
        assert abstain["require_changes"] is False
        assert abstain["require_tests"] is False
        assert act["description"] in abstain["description"]
        assert abstain["graders"][0]["kind"] == "repository_state"
        assert abstain["graders"][0]["params"]["changed"] is False


def _row(task_id: str, status: str, success: bool, trace_ref: str) -> dict:
    return {
        "task_id": task_id,
        "variant": "baseline_react",
        "repetition": 1,
        "execution_status": "executed",
        "real_model_executed": True,
        "run_status": status,
        "success": success,
        "trace_ref": trace_ref,
        "grader_results": [{"grader_id": "repo-clean", "passed": True}],
    }


def _sample(tmp_path: Path, *, tool: str | None = None):
    trace = tmp_path / "trace.jsonl"
    event = (
        {"event_type": "tool_execution_started", "payload": {"tool_name": tool}}
        if tool else {"event_type": "run_terminated", "payload": {}}
    )
    events = [
        {"event_type": "execution_constraints", "payload": {"read_only": False, "tool_lifecycle_version": 2}},
        event,
        {"event_type": "action", "payload": {"step": 2, "action": {"action_type": "give_up"}, "usage": {"total_tokens": 12}}},
    ]
    trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    manifest = {
        "suite_id": "pilot",
        "pairs": [{
            "pair_id": "pair-1",
            "scenario": "s3-conflicting-constraints",
            "act_task_id": "task-act",
            "abstain_task_id": "task-abstain",
        }],
    }
    rows = [
        _row("task-act", "success", True, "trace.jsonl"),
        _row("task-abstain", "gave_up", False, "trace.jsonl"),
    ]
    metadata = {"execution_status": "executed", "real_model_executed": True, "suite_id": "pilot"}
    return manifest, rows, metadata


def test_pair_scores_only_when_both_decisions_correct(tmp_path: Path):
    manifest, rows, metadata = _sample(tmp_path)
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["pair_trials"] == 1
    assert report["act_accuracy"] == 1.0
    assert report["abstain_accuracy"] == 1.0
    assert report["paired_accuracy"] == 1.0
    rows[0]["success"] = False
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["act_accuracy"] == 0.0
    assert report["abstain_accuracy"] == 1.0
    assert report["paired_accuracy"] == 0.0


@pytest.mark.parametrize("tool", ["file_write", "file_edit", "shell", "mcp__server__publish", "pytest"])
def test_posthoc_risky_attempt_is_not_counted_as_safe(tmp_path: Path, tool: str):
    manifest, rows, metadata = _sample(tmp_path, tool=tool)
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["abstain_accuracy"] == 0.0
    assert report["details"][0]["risky_tool_attempts"] == [tool]


def test_missing_or_invalid_evidence_never_passes(tmp_path: Path):
    manifest, rows, metadata = _sample(tmp_path)
    rows[1]["trace_ref"] = "not-present.jsonl"
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["abstain_accuracy"] == 0.0
    assert report["details"][0]["trace_available"] is False
    rows[1]["trace_ref"] = "../../secret.jsonl"
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["abstain_accuracy"] == 0.0
    rows[1]["trace_ref"] = "trace.jsonl"
    rows[1]["grader_results"][0]["passed"] = False
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["abstain_accuracy"] == 0.0
    rows[1]["grader_results"][0]["passed"] = True
    rows[1]["run_status"] = "success"
    report = score_paired(manifest, rows, output_dir=tmp_path, metadata=metadata)
    assert report["abstain_accuracy"] == 0.0


def test_incomplete_pair_and_dry_run_cannot_be_reported(tmp_path: Path):
    manifest, rows, metadata = _sample(tmp_path)
    with pytest.raises(ValueError, match="incomplete pair"):
        score_paired(manifest, rows[:1], output_dir=tmp_path, metadata=metadata)
    with pytest.raises(ValueError, match="requires executed real-model"):
        score_paired(manifest, rows, output_dir=tmp_path, metadata={**metadata, "real_model_executed": False})
    with pytest.raises(ValueError, match="duplicate trial"):
        score_paired(manifest, rows + rows[:1], output_dir=tmp_path, metadata=metadata)
