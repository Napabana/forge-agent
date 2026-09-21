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
