from __future__ import annotations

import json
from pathlib import Path

from agent.task import RunResult, RunStatus
from evals.coding_agent.graders import GraderContext, grade
from evals.coding_agent.schema import GraderSpec
from experience.candidate import DeterministicCandidateGenerator
from experience.schema import ExperiencePattern, PatternType, TrajectoryRef
import scripts.run_real_skill_evolution_eval as final_gate


def _run_result(trace: Path) -> RunResult:
    return RunResult(
        task_id="fixture",
        status=RunStatus.SUCCESS,
        summary="done",
        steps_taken=4,
        trace_path=str(trace),
    )


def _write_trace(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _motif_spec(skill: str = "candidate-skill") -> GraderSpec:
    return GraderSpec(
        "motif",
        "recovery_motif",
        {
            "failure_category": "no_progress",
            "recovery_strategy": "change_approach",
            "skill_name": skill,
            "next_operation": "INSPECT",
        },
        required=True,
    )


def test_recovery_motif_grader_requires_ordered_trigger_load_and_inspect(
    tmp_path: Path,
):
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            {"event_type": "failure_classified", "payload": {"category": "no_progress"}},
            {
                "event_type": "recovery_selected",
                "payload": {"strategy": "change_approach"},
            },
            {"event_type": "skill_loaded", "payload": {"skill": "candidate-skill"}},
            {"event_type": "tool_execution_started", "payload": {"tool_name": "file_read"}},
        ],
    )
    result = grade(
        _motif_spec(),
        GraderContext(repo=tmp_path, run_result=_run_result(trace)),
    )
    assert result.passed is True
    assert result.evidence["next_operation"] == "INSPECT"


def test_recovery_motif_grader_rejects_early_load_or_wrong_next_action(
    tmp_path: Path,
):
    early = tmp_path / "early.jsonl"
    _write_trace(
        early,
        [
            {"event_type": "skill_loaded", "payload": {"skill": "candidate-skill"}},
            {"event_type": "failure_classified", "payload": {"category": "no_progress"}},
            {
                "event_type": "recovery_selected",
                "payload": {"strategy": "change_approach"},
            },
            {"event_type": "tool_execution_started", "payload": {"tool_name": "file_read"}},
        ],
    )
    assert grade(
        _motif_spec(),
        GraderContext(repo=tmp_path, run_result=_run_result(early)),
    ).passed is False

    wrong = tmp_path / "wrong.jsonl"
    _write_trace(
        wrong,
        [
            {"event_type": "failure_classified", "payload": {"category": "no_progress"}},
            {
                "event_type": "recovery_selected",
                "payload": {"strategy": "change_approach"},
            },
            {"event_type": "skill_loaded", "payload": {"skill": "candidate-skill"}},
            {"event_type": "tool_execution_started", "payload": {"tool_name": "file_edit"}},
        ],
    )
    assert grade(
        _motif_spec(),
        GraderContext(repo=tmp_path, run_result=_run_result(wrong)),
    ).passed is False


def _write_mining_report(tmp_path: Path) -> tuple[Path, str]:
    refs = (
        TrajectoryRef(
            run_id="run-a",
            task_id="task-a",
            trace_ref="/tmp/a.jsonl",
            trace_sha256="a" * 64,
            run_status="success",
            acceptance_status="passed",
            termination_reason="completion_satisfied",
        ),
        TrajectoryRef(
            run_id="run-b",
            task_id="task-b",
            trace_ref="/tmp/b.jsonl",
            trace_sha256="b" * 64,
            run_status="success",
            acceptance_status="passed",
            termination_reason="completion_satisfied",
        ),
    )
    pattern = ExperiencePattern.build(
        pattern_type=PatternType.RECOVERY_WORKFLOW,
        signature=(
            "failure:no_progress",
            "recovery:change_approach",
            "INSPECT",
        ),
        source_trajectories=refs,
        failure_categories=("no_progress",),
        recovery_strategies=("change_approach",),
    )
    candidate = DeterministicCandidateGenerator().generate(pattern)
    artifact_dir = tmp_path / "candidate"
    artifact_dir.mkdir()
    (artifact_dir / "candidate.json").write_text(
        json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "mode": "mine_only",
        "mining_strategy": "recovery_motif_v2",
        "patterns": [pattern.to_dict()],
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "candidate_version": candidate.candidate_version,
                "skill_name": candidate.skill_name,
                "description": candidate.description,
                "content_hash": candidate.content_hash,
                "pattern_id": pattern.pattern_id,
                "pattern_type": pattern.pattern_type.value,
                "evidence_count": pattern.evidence_count,
                "promotion_min_evidence_count": 2,
                "promotion_evidence_ready": True,
                "artifact_dir": str(artifact_dir),
            }
        ],
    }
    report_path = tmp_path / "mining_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path, pattern.pattern_id


def test_real_final_gate_defaults_to_zero_api_dry_run(
    tmp_path: Path,
    capsys,
):
    report, pattern_id = _write_mining_report(tmp_path)
    output = tmp_path / "real-eval"

    code = final_gate.main(
        [
            "--mining-report",
            str(report),
            "--pattern-id",
            pattern_id,
            "--output-dir",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["provider_calls"] == 0
    assert payload["paired_trials"] == 8
    assert payload["pattern_signature"] == [
        "failure:no_progress",
        "recovery:change_approach",
        "INSPECT",
    ]
    assert payload["process_grader"]["required"] is True
    assert payload["planning_mode"] == "off"
    assert payload["recovery_mode"] == "structured"
    assert output.exists() is False
