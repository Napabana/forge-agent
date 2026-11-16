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
    assert payload["process_grader"]["trial_blocking"] is False
    assert payload["process_grader"]["promotion_required"] is True
    assert payload["planning_mode"] == "off"
    assert payload["recovery_mode"] == "structured"
    assert output.exists() is False


def _trial_payload(
    *,
    task_id: str,
    variant: str,
    candidate_skill: str | None = None,
    fail_process: bool = False,
) -> dict:
    graders = [
        {
            "grader_id": "behavior",
            "kind": "command",
            "passed": True,
            "required": True,
            "detail": "ok",
            "evidence": {},
        }
    ]
    success = True
    if candidate_skill is not None:
        graders.append(
            {
                "grader_id": "evolution-skill-trigger",
                "kind": "skill_selection",
                "passed": not fail_process,
                "required": False,
                "detail": "selection",
                "evidence": {
                    "selected_skills": (
                        [] if fail_process else [candidate_skill]
                    )
                },
            }
        )
        if fail_process:
            graders.append(
                {
                    "grader_id": "evolution-recovery-motif",
                    "kind": "recovery_motif",
                    "passed": False,
                    "required": True,
                    "detail": "historical blocking process grader",
                    "evidence": {},
                }
            )
            # Historical final-gate artifacts used combined outcome+process success.
            success = False
    return {
        "suite_id": "p2-5-recovery-motif-real-gate",
        "task_id": task_id,
        "trial_id": f"{task_id}--{variant}--r001",
        "variant": variant,
        "repetition": 1,
        "execution_status": "executed",
        "evidence_kind": "real_model",
        "real_model_executed": True,
        "run_status": "success",
        "termination_reason": "completion_satisfied",
        "acceptance_status": "passed",
        "success": success,
        "metrics": {
            "steps": 5,
            "total_tokens": 1000,
            "provider_usage": {},
            "wall_time_seconds": 1.0,
        },
        "grader_results": graders,
        "trace_ref": None,
        "patch_ref": None,
        "final_state_ref": None,
        "error": None,
    }


def test_replay_existing_separates_outcome_from_historical_process_failure(
    tmp_path: Path,
    capsys,
):
    report, pattern_id = _write_mining_report(tmp_path)
    output = tmp_path / "real-eval"
    baseline_dir = output / "baseline"
    candidate_dir = output / "candidate"
    baseline_dir.mkdir(parents=True)
    candidate_dir.mkdir(parents=True)

    candidate_raw = json.loads(
        (tmp_path / "candidate" / "candidate.json").read_text(encoding="utf-8")
    )
    skill_name = candidate_raw["skill_name"]
    task_ids = [
        "target-normalize-label",
        "should-trigger-timeout-policy",
        "should-not-trigger-config",
        "non-regression-triple",
    ]
    baseline_rows = [
        _trial_payload(task_id=task_id, variant="evolution_baseline")
        for task_id in task_ids
    ]
    candidate_rows = [
        _trial_payload(
            task_id=task_id,
            variant="evolution_candidate",
            candidate_skill=(
                skill_name
                if task_id
                in {"target-normalize-label", "should-trigger-timeout-policy"}
                else None
            ),
            fail_process=(
                task_id
                in {"target-normalize-label", "should-trigger-timeout-policy"}
            ),
        )
        for task_id in task_ids
    ]
    (baseline_dir / "raw.jsonl").write_text(
        "\n".join(json.dumps(row) for row in baseline_rows) + "\n",
        encoding="utf-8",
    )
    (candidate_dir / "raw.jsonl").write_text(
        "\n".join(json.dumps(row) for row in candidate_rows) + "\n",
        encoding="utf-8",
    )

    code = final_gate.main(
        [
            "--mining-report",
            str(report),
            "--pattern-id",
            pattern_id,
            "--output-dir",
            str(output),
            "--replay-existing",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "replay_existing"
    assert payload["provider_calls"] == 0
    assert payload["promotion_status"] == "reject"
    by_task = {item["task_id"]: item for item in payload["cases"]}
    assert by_task["target-normalize-label"]["candidate_success"] is True
    assert by_task["should-trigger-timeout-policy"]["candidate_success"] is True
    assert by_task["target-normalize-label"]["candidate_process_passed"] is False
    assert by_task["should-trigger-timeout-policy"]["candidate_process_passed"] is False
    reasons = payload["promotion_reasons"]
    assert not any("candidate outcome failed" in reason for reason in reasons)
    assert not any("regressed a baseline-success case" in reason for reason in reasons)
    assert any("candidate was not loaded when required" in reason for reason in reasons)
    assert any("candidate process grader failed" in reason for reason in reasons)
