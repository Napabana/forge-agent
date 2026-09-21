from __future__ import annotations

import json
from pathlib import Path

import scripts.run_skill_evolution as driver


ROOT = Path(__file__).resolve().parents[1]
TRAJECTORIES = ROOT / "evals" / "fixtures" / "skill_evolution" / "trajectories"


def test_batch_summary_mines_matching_real_trace_shape_without_api(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = tmp_path / "batch_summary.json"
    summary.write_text(
        json.dumps({
            "eligible_traces_for_p2_5": [
                str(TRAJECTORIES / "success-a.jsonl"),
                str(TRAJECTORIES / "success-b.jsonl"),
            ]
        }),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    code = driver.main([
        "--repo",
        str(repo),
        "--batch-summary",
        str(summary),
        "--mine-only",
        "--no-store",
        "--output-dir",
        str(output),
    ])

    assert code == 0
    report = json.loads((output / "mining_report.json").read_text(encoding="utf-8"))
    assert report["provider_calls"] == 0
    assert report["evaluation_executed"] is False
    assert report["mining_strategy"] == "recovery_motif_v2"
    assert report["candidate_renderer"] == "progressive_disclosure_v2"
    assert report["trajectory_count"] == 2
    assert report["eligible_trajectory_count"] == 2
    assert report["pattern_count"] == 1
    assert report["candidate_count"] == 1
    assert report["patterns"][0]["evidence_count"] == 2
    assert report["candidates"][0]["promotion_evidence_ready"] is True
    candidate_dir = Path(report["candidates"][0]["artifact_dir"])
    assert (candidate_dir / "candidate.json").is_file()
    assert (candidate_dir / "SKILL.md").is_file()

    stdout = capsys.readouterr().out
    assert "OFFLINE / API calls = 0" in stdout
    assert "2 total / 2 eligible" in stdout
    assert "gate-ready: yes" in stdout


def test_candidate_store_is_idempotent_for_same_trace_evidence(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_args = [
        "--trace",
        str(TRAJECTORIES / "success-a.jsonl"),
        "--trace",
        str(TRAJECTORIES / "success-b.jsonl"),
    ]

    first_out = tmp_path / "first"
    assert driver.main([
        "--repo",
        str(repo),
        *trace_args,
        "--mine-only",
        "--output-dir",
        str(first_out),
    ]) == 0
    first = json.loads((first_out / "mining_report.json").read_text(encoding="utf-8"))
    assert first["candidates"][0]["candidate_store_reused"] is False

    second_out = tmp_path / "second"
    assert driver.main([
        "--repo",
        str(repo),
        *trace_args,
        "--mine-only",
        "--output-dir",
        str(second_out),
    ]) == 0
    second = json.loads((second_out / "mining_report.json").read_text(encoding="utf-8"))
    assert second["candidates"][0]["candidate_store_reused"] is True
    assert (
        first["candidates"][0]["candidate_id"]
        == second["candidates"][0]["candidate_id"]
    )


def test_single_evidence_candidate_is_reported_not_gate_ready(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "out"

    code = driver.main([
        "--repo",
        str(repo),
        "--trace",
        str(TRAJECTORIES / "success-a.jsonl"),
        "--mine-only",
        "--no-store",
        "--output-dir",
        str(output),
    ])

    assert code == 0
    report = json.loads((output / "mining_report.json").read_text(encoding="utf-8"))
    assert report["pattern_count"] == 1
    assert report["candidates"][0]["evidence_count"] == 1
    assert report["candidates"][0]["promotion_evidence_ready"] is False
    assert "Do not spend real-model evaluation tokens yet" in capsys.readouterr().out


def test_ineligible_trace_is_visible_but_not_mined(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "out"

    code = driver.main([
        "--repo",
        str(repo),
        "--trace",
        str(TRAJECTORIES / "canceled.jsonl"),
        "--mine-only",
        "--no-store",
        "--output-dir",
        str(output),
    ])

    assert code == 0
    report = json.loads((output / "mining_report.json").read_text(encoding="utf-8"))
    assert report["trajectory_count"] == 1
    assert report["eligible_trajectory_count"] == 0
    assert report["pattern_count"] == 0
    assert report["candidate_count"] == 0
    assert report["trajectories"][0]["eligible"] is False


def test_requires_explicit_mine_only(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    try:
        driver.main([
            "--repo",
            str(repo),
            "--trace",
            str(TRAJECTORIES / "success-a.jsonl"),
        ])
    except SystemExit as exc:
        assert "only offline --mine-only is supported" in str(exc)
    else:
        raise AssertionError("driver must require explicit --mine-only")


def _write_recovery_trace(
    path: Path,
    *,
    task_id: str,
    run_id: str,
    before_failure_tool: str,
    after_recovery_tool: str,
) -> None:
    rows = [
        {
            "event_type": "task_start",
            "task_id": task_id,
            "payload": {"run_id": run_id, "task": {"task_id": task_id}},
        },
        {
            "event_type": "tool_execution_started",
            "task_id": task_id,
            "payload": {"tool_name": before_failure_tool},
        },
        {
            "event_type": "failure_classified",
            "task_id": task_id,
            "payload": {"category": "test_failure"},
        },
        {
            "event_type": "recovery_selected",
            "task_id": task_id,
            "payload": {"strategy": "inspect"},
        },
        {
            "event_type": "tool_execution_started",
            "task_id": task_id,
            "payload": {"tool_name": "file_read"},
        },
        {
            "event_type": "tool_execution_started",
            "task_id": task_id,
            "payload": {"tool_name": after_recovery_tool},
        },
        {
            "event_type": "task_complete",
            "task_id": task_id,
            "payload": {"steps": 4},
        },
        {
            "event_type": "acceptance",
            "task_id": task_id,
            "payload": {"acceptance_status": "passed"},
        },
        {
            "event_type": "run_terminated",
            "task_id": task_id,
            "payload": {
                "run_id": run_id,
                "status": "success",
                "termination_reason": "completion_satisfied",
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_driver_groups_matching_recovery_motif_across_different_workflows(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_recovery_trace(
        left,
        task_id="left",
        run_id="run-left",
        before_failure_tool="pytest",
        after_recovery_tool="file_write",
    )
    _write_recovery_trace(
        right,
        task_id="right",
        run_id="run-right",
        before_failure_tool="shell",
        after_recovery_tool="file_edit",
    )
    summary = tmp_path / "batch_summary.json"
    summary.write_text(
        json.dumps({
            "eligible_traces_for_p2_5": [str(left), str(right)]
        }),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    assert driver.main([
        "--repo",
        str(repo),
        "--batch-summary",
        str(summary),
        "--mine-only",
        "--no-store",
        "--output-dir",
        str(output),
    ]) == 0

    report = json.loads(
        (output / "mining_report.json").read_text(encoding="utf-8")
    )
    matching = [
        pattern
        for pattern in report["patterns"]
        if pattern["signature"]
        == [
            "failure:test_failure",
            "recovery:inspect",
            "INSPECT",
        ]
    ]
    assert len(matching) == 1
    assert matching[0]["evidence_count"] == 2
    candidate = next(
        item
        for item in report["candidates"]
        if item["pattern_id"] == matching[0]["pattern_id"]
    )
    assert candidate["promotion_evidence_ready"] is True
