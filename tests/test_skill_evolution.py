from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core import AgentConfig
from agent.runner import ExecutionRunner
from agent.task import Action, ActionType, ToolCall
from evals.coding_agent.schema import EvaluationSuite
from experience.candidate import DeterministicCandidateGenerator
from experience.evaluation import evaluate_candidate
from experience.promotion import (
    PromotionGate,
    PromotionGateConfig,
    PromotionManager,
)
from experience.schema import (
    CandidateStatus,
    EvaluationCase,
    EvaluationRecord,
    EvaluationRole,
    ExperiencePattern,
    NormalizedTrajectory,
    PatternType,
    PromotionStatus,
    SkillCandidate,
    TrajectoryRef,
)
from experience.store import CandidateStore
from experience.trajectory import ExperienceMiner, load_trajectory
from llm.base import MockBackend
from skills.catalog import SkillCatalog
from tools.base import ToolRegistry
from tools.file_tool import FileWriteTool

ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "skill_evolution"
TRAJECTORY_ROOT = FIXTURE_ROOT / "trajectories"
SUITE = FIXTURE_ROOT / "suite.json"


def _load(name: str):
    return load_trajectory(TRAJECTORY_ROOT / name).normalized


def _success_pattern() -> ExperiencePattern:
    patterns = ExperienceMiner().mine((
        _load("success-a.jsonl"),
        _load("success-b.jsonl"),
    ))
    assert len(patterns) == 1
    return patterns[0]


def _candidate(*, name: str = "verified-bug-fix") -> SkillCandidate:
    return DeterministicCandidateGenerator().generate(
        _success_pattern(),
        skill_name=name,
    )


def _case(
    task_id: str,
    role: EvaluationRole,
    *,
    baseline_success: bool = True,
    candidate_success: bool = True,
    loaded: bool | None = None,
    process_passed: bool = True,
    baseline_steps: int = 2,
    candidate_steps: int = 2,
    baseline_tokens: int = 100,
    candidate_tokens: int = 100,
    evaluation_failed: bool = False,
    failure_reason: str | None = None,
) -> EvaluationCase:
    if loaded is None:
        loaded = role in {
            EvaluationRole.TARGET,
            EvaluationRole.SHOULD_TRIGGER,
        }
    return EvaluationCase(
        task_id=task_id,
        repetition=1,
        role=role,
        baseline_success=baseline_success,
        candidate_success=candidate_success,
        baseline_required_graders_passed=baseline_success,
        candidate_required_graders_passed=candidate_success,
        candidate_loaded=loaded,
        candidate_process_passed=process_passed,
        baseline_steps=baseline_steps,
        candidate_steps=candidate_steps,
        baseline_tokens=baseline_tokens,
        candidate_tokens=candidate_tokens,
        evaluation_failed=evaluation_failed,
        failure_reason=failure_reason,
    )


def _record(
    candidate: SkillCandidate,
    *overrides: EvaluationCase,
) -> EvaluationRecord:
    cases = overrides or (
        _case("target", EvaluationRole.TARGET),
        _case("trigger", EvaluationRole.SHOULD_TRIGGER),
        _case(
            "non-trigger",
            EvaluationRole.SHOULD_NOT_TRIGGER,
            loaded=False,
        ),
        _case(
            "regression",
            EvaluationRole.NON_REGRESSION,
            loaded=False,
        ),
    )
    return EvaluationRecord.build(
        candidate=candidate,
        suite_id="fixture-suite",
        baseline_variant="baseline",
        candidate_variant="candidate",
        cases=tuple(cases),
        created_at="2026-09-19T00:00:00+00:00",
    )


def test_successful_acceptance_pass_is_eligible_and_failed_cancel_infra_are_not():
    success = _load("success-a.jsonl")
    canceled = _load("canceled.jsonl")
    infrastructure = _load("infrastructure.jsonl")
    assert success.eligible is True
    assert (
        success.eligibility_reason
        == "success_with_acceptance_pass"
    )
    assert (
        canceled.eligible is False
        and "canceled" in canceled.eligibility_reason
    )
    assert (
        infrastructure.eligible is False
        and "failed" in infrastructure.eligibility_reason
    )


def test_success_without_independent_acceptance_is_not_mined(
    tmp_path: Path,
):
    trace = tmp_path / "unverified.jsonl"
    rows = [
        {
            "event_type": "task_start",
            "task_id": "u",
            "payload": {
                "run_id": "run-u",
                "task": {"task_id": "u"},
            },
        },
        {
            "event_type": "tool_execution_started",
            "task_id": "u",
            "payload": {"tool_name": "file_read"},
        },
        {
            "event_type": "task_complete",
            "task_id": "u",
            "payload": {"steps": 1},
        },
        {
            "event_type": "acceptance",
            "task_id": "u",
            "payload": {"acceptance_status": "not_requested"},
        },
        {
            "event_type": "run_terminated",
            "task_id": "u",
            "payload": {
                "run_id": "run-u",
                "status": "success",
                "termination_reason": "completion_satisfied",
            },
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    trajectory = load_trajectory(trace).normalized
    assert trajectory.eligible is False
    assert (
        trajectory.eligibility_reason
        == "acceptance_status=not_requested"
    )
    assert ExperienceMiner().mine((trajectory,)) == ()


def test_missing_canonical_terminal_provenance_is_rejected(
    tmp_path: Path,
):
    trace = tmp_path / "bad.jsonl"
    trace.write_text(
        json.dumps({
            "event_type": "task_start",
            "task_id": "x",
            "payload": {"run_id": "r"},
        })
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="task_start and run_terminated",
    ):
        load_trajectory(trace)


def _normalized_recovery(
    *,
    run_id: str,
    workflow: tuple[str, ...],
    failures: tuple[str, ...],
    recoveries: tuple[str, ...],
) -> NormalizedTrajectory:
    return NormalizedTrajectory(
        ref=TrajectoryRef(
            run_id=run_id,
            task_id=f"task-{run_id}",
            trace_ref=f"/tmp/{run_id}.jsonl",
            trace_sha256=(run_id[-1] if run_id[-1] in "abcdef0123456789" else "a") * 64,
            run_status="success",
            acceptance_status="passed",
            termination_reason="completion_satisfied",
        ),
        workflow=workflow,
        failure_categories=failures,
        recovery_strategies=recoveries,
        eligible=True,
        eligibility_reason="success_with_acceptance_pass",
    )


def test_recovery_pattern_uses_bounded_typed_motifs():
    trajectory = _load("recovery.jsonl")
    assert trajectory.eligible is True
    assert trajectory.failure_categories == ("test_failure",)
    assert trajectory.recovery_strategies == (
        "inspect",
        "replan",
    )
    assert "REPLAN" in trajectory.workflow

    patterns = ExperienceMiner().mine((trajectory,))
    signatures = {pattern.signature for pattern in patterns}
    assert (
        "failure:test_failure",
        "recovery:inspect",
        "INSPECT",
    ) in signatures
    assert (
        "failure:test_failure",
        "recovery:replan",
        "REPLAN",
    ) in signatures
    assert all(
        pattern.pattern_type is PatternType.RECOVERY_WORKFLOW
        for pattern in patterns
    )
    assert all(pattern.evidence_count == 1 for pattern in patterns)


def test_recovery_motifs_group_across_different_full_workflows():
    first = _normalized_recovery(
        run_id="run-a",
        workflow=(
            "PLAN",
            "TEST",
            "FAIL:test_failure",
            "RECOVER:inspect",
            "INSPECT",
            "EDIT",
            "TEST",
            "FINISH",
        ),
        failures=("test_failure",),
        recoveries=("inspect",),
    )
    second = _normalized_recovery(
        run_id="run-b",
        workflow=(
            "SHELL",
            "INSPECT",
            "FAIL:test_failure",
            "RECOVER:inspect",
            "INSPECT",
            "REPLAN",
            "EDIT",
            "TEST",
            "FINISH",
        ),
        failures=("test_failure",),
        recoveries=("inspect",),
    )

    patterns = ExperienceMiner().mine((first, second))
    matching = [
        pattern
        for pattern in patterns
        if pattern.signature
        == (
            "failure:test_failure",
            "recovery:inspect",
            "INSPECT",
        )
    ]
    assert len(matching) == 1
    assert matching[0].evidence_count == 2
    assert matching[0].failure_categories == ("test_failure",)
    assert matching[0].recovery_strategies == ("inspect",)


def test_same_trace_repeated_recovery_motif_counts_as_one_evidence():
    repeated = _normalized_recovery(
        run_id="run-c",
        workflow=(
            "TEST",
            "FAIL:test_failure",
            "RECOVER:inspect",
            "INSPECT",
            "TEST",
            "FAIL:test_failure",
            "RECOVER:inspect",
            "INSPECT",
            "EDIT",
            "TEST",
            "FINISH",
        ),
        failures=("test_failure", "test_failure"),
        recoveries=("inspect", "inspect"),
    )
    patterns = ExperienceMiner().mine((repeated,))
    matching = [
        pattern
        for pattern in patterns
        if pattern.signature
        == (
            "failure:test_failure",
            "recovery:inspect",
            "INSPECT",
        )
    ]
    assert len(matching) == 1
    assert matching[0].evidence_count == 1


def test_recovery_candidate_metadata_preserves_progressive_disclosure():
    pattern = ExperienceMiner().mine((
        _normalized_recovery(
            run_id="run-d",
            workflow=(
                "TEST",
                "FAIL:test_failure",
                "RECOVER:inspect",
                "INSPECT",
                "EDIT",
                "FINISH",
            ),
            failures=("test_failure",),
            recoveries=("inspect",),
        ),
    ))[0]
    candidate = DeterministicCandidateGenerator().generate(pattern)
    assert "# Recovery Motif" in candidate.instructions
    assert "classifies test_failure" in candidate.description
    assert "selects inspect" in candidate.description
    assert "next semantic action is INSPECT" not in candidate.description
    assert "load this Skill before choosing the next semantic action" in candidate.description
    assert "Observed failure category: `test_failure`." in candidate.instructions
    assert "Observed recovery strategy: `inspect`." in candidate.instructions
    assert "Inspect the failure evidence" in candidate.instructions
    assert "Inspect the smallest relevant repository evidence" in candidate.instructions


def test_unrelated_workflows_are_not_merged():
    patterns = ExperienceMiner().mine((
        _load("success-a.jsonl"),
        _load("success-b.jsonl"),
        _load("unrelated.jsonl"),
    ))
    assert sorted(
        pattern.evidence_count for pattern in patterns
    ) == [1, 2]


def test_mining_is_deterministic_and_input_order_independent():
    left = ExperienceMiner().mine((
        _load("success-a.jsonl"),
        _load("success-b.jsonl"),
    ))
    right = ExperienceMiner().mine((
        _load("success-b.jsonl"),
        _load("success-a.jsonl"),
    ))
    assert [item.pattern_id for item in left] == [
        item.pattern_id for item in right
    ]
    assert [item.to_dict() for item in left] == [
        item.to_dict() for item in right
    ]


def test_pattern_identity_ignores_local_trace_path_and_deduplicates_same_evidence():
    source = _load("success-a.jsonl").ref
    relocated = type(source)(
        **{
            **source.__dict__,
            "trace_ref": "/different/machine/trace.jsonl",
        }
    )
    left = ExperiencePattern.build(
        pattern_type=PatternType.SUCCESSFUL_WORKFLOW,
        signature=("INSPECT", "EDIT", "TEST", "FINISH"),
        source_trajectories=(source, source),
    )
    right = ExperiencePattern.build(
        pattern_type=PatternType.SUCCESSFUL_WORKFLOW,
        signature=("INSPECT", "EDIT", "TEST", "FINISH"),
        source_trajectories=(relocated,),
    )
    assert left.pattern_id == right.pattern_id
    assert left.evidence_count == right.evidence_count == 1


def test_candidate_identity_version_and_hash_are_deterministic():
    pattern = _success_pattern()
    first = SkillCandidate.build(
        skill_name="verified-bug-fix",
        description="A verified workflow.",
        instructions="Inspect.\nEdit.\nVerify.",
        pattern=pattern,
        created_at="2026-09-19T00:00:00+00:00",
    )
    second = SkillCandidate.build(
        skill_name="verified-bug-fix",
        description="A verified workflow.",
        instructions="Inspect.\nEdit.\nVerify.",
        pattern=pattern,
        created_at="2026-09-20T00:00:00+00:00",
    )
    assert first.candidate_id == second.candidate_id
    assert first.candidate_version == second.candidate_version == 1
    assert first.content_hash == second.content_hash


def test_candidate_requires_complete_provenance():
    candidate = _candidate()
    with pytest.raises(
        ValueError,
        match="parent skill provenance must be complete",
    ):
        SkillCandidate(
            **{
                **candidate.__dict__,
                "candidate_id": "candidate-parent-test",
                "parent_skill_name": candidate.skill_name,
                "parent_skill_version": 1,
                "parent_skill_hash": None,
            }
        )


def test_candidate_store_isolated_from_normal_skill_catalog(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    candidate = _candidate()
    store.save_candidate(candidate)
    assert (
        SkillCatalog.discover(repo).get(candidate.skill_name)
        is None
    )
    assert (
        store.load_candidate(
            candidate.candidate_id,
            1,
        ).content_hash
        == candidate.content_hash
    )


def test_candidate_store_duplicate_corrupt_bound_and_project_boundary(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = _candidate()
    store = CandidateStore(repo)
    path = store.save_candidate(candidate)
    with pytest.raises(FileExistsError):
        store.save_candidate(candidate)
    (path / "candidate.json").write_text(
        "{not-json",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="corrupt candidate metadata",
    ):
        store.load_candidate(
            candidate.candidate_id,
            candidate.candidate_version,
        )
    with pytest.raises(
        ValueError,
        match="repository boundary",
    ):
        CandidateStore(repo, tmp_path / "outside")
    with pytest.raises(
        ValueError,
        match="unsafe candidate id",
    ):
        store.load_candidate("../escape", 1)

    huge = SkillCandidate.build(
        skill_name="huge-candidate",
        description="bounded",
        instructions="x" * (129 * 1024),
        pattern=_success_pattern(),
    )
    with pytest.raises(
        ValueError,
        match="SKILL.md exceeds",
    ):
        CandidateStore(
            repo,
            ".forge-agent/other-experience",
        ).save_candidate(huge)


def _write_task(repo: Path, task_id: str) -> Action:
    contents = {
        "target-bug-fix": (
            "calc.py",
            (
                "def subtract(left, right):\n"
                "    return left - right\n"
            ),
        ),
        "should-trigger-bug-fix": (
            "number.py",
            (
                "def is_even(value):\n"
                "    return value % 2 == 0\n"
            ),
        ),
        "should-not-trigger-config": (
            "config.py",
            "API_VERSION = 2\n",
        ),
        "non-regression-feature": (
            "math_utils.py",
            (
                "def double(value):\n"
                "    return value * 2\n\n\n"
                "def triple(value):\n"
                "    return value * 3\n"
            ),
        ),
    }
    path, body = contents[task_id]
    return Action(
        ActionType.TOOL_CALL,
        "apply deterministic fixture change",
        ToolCall(
            "file_write",
            {"path": path, "content": body},
        ),
    )


def _runner_factory_builder(candidate: SkillCandidate):
    def builder(skill_root: Path | None):
        def factory(task, repo, trace_dir, trial_id):
            actions: list[Action] = []
            if (
                skill_root is not None
                and task.task_id
                in {
                    "target-bug-fix",
                    "should-trigger-bug-fix",
                }
            ):
                actions.append(Action(
                    ActionType.TOOL_CALL,
                    "load relevant candidate",
                    ToolCall(
                        "skill_load",
                        {"name": candidate.skill_name},
                    ),
                ))
            actions.extend([
                _write_task(repo, task.task_id),
                Action(
                    ActionType.FINISH,
                    "fixture complete",
                    message="done",
                ),
            ])
            backend = MockBackend(actions)
            registry = ToolRegistry().register(
                FileWriteTool(workspace=str(repo))
            )
            return ExecutionRunner(
                backend=backend,
                registry=registry,
                config=AgentConfig(
                    max_steps=8,
                    repo_map_mode="none",
                    skills_enabled=skill_root is not None,
                    skills_global_dir=(
                        str(skill_root)
                        if skill_root is not None
                        else None
                    ),
                ),
                log_dir=str(trace_dir),
            )

        return factory

    return builder


def test_candidate_evaluation_reuses_existing_harness_and_observes_trigger_contract(
    tmp_path: Path,
):
    candidate = _candidate()
    suite = EvaluationSuite.load(SUITE)
    record = evaluate_candidate(
        candidate=candidate,
        suite=suite,
        output_dir=tmp_path / "eval",
        runner_factory_builder=_runner_factory_builder(
            candidate
        ),
    )
    by_role = {
        case.role: case
        for case in record.cases
    }
    assert (
        by_role[EvaluationRole.TARGET].candidate_loaded
        is True
    )
    assert (
        by_role[
            EvaluationRole.SHOULD_TRIGGER
        ].candidate_loaded
        is True
    )
    assert (
        by_role[
            EvaluationRole.SHOULD_NOT_TRIGGER
        ].candidate_loaded
        is False
    )
    assert all(
        case.candidate_success for case in record.cases
    )
    assert all(
        case.baseline_trial_id
        and case.candidate_trial_id
        for case in record.cases
    )
    assert all(
        case.baseline_trace_ref
        and case.candidate_trace_ref
        for case in record.cases
    )
    assert (
        tmp_path
        / "eval"
        / "baseline"
        / "report.json"
    ).is_file()
    assert (
        tmp_path
        / "eval"
        / "candidate"
        / "report.json"
    ).is_file()
    assert (
        tmp_path
        / "eval"
        / "evaluation.json"
    ).is_file()


def test_promotion_gate_pass_and_required_statuses():
    candidate = _candidate()
    decision = PromotionGate().evaluate(
        candidate,
        _record(candidate),
    )
    assert decision.status is PromotionStatus.PASS

    insufficient_candidate = SkillCandidate.build(
        skill_name="single-evidence",
        description="single",
        instructions="Inspect and verify.",
        pattern=ExperienceMiner().mine((
            _load("unrelated.jsonl"),
        ))[0],
    )
    insufficient = PromotionGate().evaluate(
        insufficient_candidate,
        _record(insufficient_candidate),
    )
    assert (
        insufficient.status
        is PromotionStatus.INSUFFICIENT_EVIDENCE
    )

    eval_failed = PromotionGate().evaluate(
        candidate,
        _record(
            candidate,
            _case(
                "target",
                EvaluationRole.TARGET,
                evaluation_failed=True,
                failure_reason="infrastructure_error",
            ),
            _case(
                "trigger",
                EvaluationRole.SHOULD_TRIGGER,
            ),
            _case(
                "non-trigger",
                EvaluationRole.SHOULD_NOT_TRIGGER,
                loaded=False,
            ),
            _case(
                "regression",
                EvaluationRole.NON_REGRESSION,
                loaded=False,
            ),
        ),
    )
    assert (
        eval_failed.status
        is PromotionStatus.EVALUATION_FAILED
    )


def test_promotion_gate_rejects_regression_trigger_process_and_overhead():
    candidate = _candidate()
    cases = (
        _case(
            "target",
            EvaluationRole.TARGET,
            candidate_success=False,
        ),
        _case(
            "trigger",
            EvaluationRole.SHOULD_TRIGGER,
            loaded=False,
        ),
        _case(
            "non-trigger",
            EvaluationRole.SHOULD_NOT_TRIGGER,
            loaded=True,
        ),
        _case(
            "regression",
            EvaluationRole.NON_REGRESSION,
            process_passed=False,
            baseline_steps=1,
            candidate_steps=20,
            baseline_tokens=100,
            candidate_tokens=10000,
            loaded=False,
        ),
    )
    decision = PromotionGate(
        PromotionGateConfig(max_step_overhead=2)
    ).evaluate(
        candidate,
        _record(candidate, *cases),
    )
    assert decision.status is PromotionStatus.REJECT
    joined = "\n".join(decision.reasons)
    assert "candidate outcome failed" in joined
    assert "not loaded when required" in joined
    assert "should-not-trigger" in joined
    assert "step overhead" in joined
    assert "token overhead" in joined


def test_promotion_gate_rejects_stale_candidate_hash_as_evaluation_failure():
    candidate = _candidate()
    record = _record(candidate)
    stale = EvaluationRecord(
        **{
            **record.__dict__,
            "candidate_hash": "b" * 64,
            "record_id": "evaluation-stale",
        }
    )
    decision = PromotionGate().evaluate(
        candidate,
        stale,
    )
    assert (
        decision.status
        is PromotionStatus.EVALUATION_FAILED
    )


def _approve(
    store: CandidateStore,
    candidate: SkillCandidate,
    record: EvaluationRecord,
):
    store.save_candidate(candidate)
    store.set_status(
        candidate.candidate_id,
        candidate.candidate_version,
        CandidateStatus.EVALUATING,
    )
    store.save_evaluation(record)
    decision = PromotionGate().evaluate(
        candidate,
        record,
    )
    store.save_decision(decision)
    return decision


def test_explicit_promotion_enters_existing_skill_catalog_and_preserves_evidence(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    candidate = _candidate()
    record = _record(candidate)
    decision = _approve(
        store,
        candidate,
        record,
    )
    target = (
        repo
        / ".agents"
        / "skills"
        / candidate.skill_name
        / "SKILL.md"
    )
    assert not target.exists()

    promoted = PromotionManager(
        repo,
        store,
    ).promote(
        candidate,
        decision,
    )
    assert promoted == target
    assert promoted.is_file()
    assert (
        SkillCatalog.discover(repo).get(
            candidate.skill_name
        )
        is not None
    )
    assert (
        store.status(
            candidate.candidate_id,
            candidate.candidate_version,
        )
        is CandidateStatus.PROMOTED
    )
    assert (
        store.root
        / "candidates"
        / candidate.candidate_id
        / "v001"
        / "candidate.json"
    ).is_file()
    assert (
        store.root
        / "evaluations"
        / f"{record.record_id}.json"
    ).is_file()


def test_promotion_requires_pass_and_never_overwrites_manual_skill(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    candidate = _candidate()
    record = _record(candidate)
    store.save_candidate(candidate)
    store.set_status(
        candidate.candidate_id,
        candidate.candidate_version,
        CandidateStatus.EVALUATING,
    )
    store.save_evaluation(record)
    rejected = PromotionGate().evaluate(
        candidate,
        _record(
            candidate,
            _case(
                "target",
                EvaluationRole.TARGET,
                candidate_success=False,
            ),
            _case(
                "trigger",
                EvaluationRole.SHOULD_TRIGGER,
            ),
            _case(
                "non-trigger",
                EvaluationRole.SHOULD_NOT_TRIGGER,
                loaded=False,
            ),
            _case(
                "regression",
                EvaluationRole.NON_REGRESSION,
                loaded=False,
            ),
        ),
    )
    with pytest.raises(
        ValueError,
        match="gate passed",
    ):
        PromotionManager(
            repo,
            store,
        ).promote(
            candidate,
            rejected,
        )

    manual = (
        repo
        / ".agents"
        / "skills"
        / candidate.skill_name
    )
    manual.mkdir(parents=True)
    (manual / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {candidate.skill_name}\n"
            "description: manual\n"
            "---\n"
            "Do not overwrite me.\n"
        ),
        encoding="utf-8",
    )
    decision = PromotionGate().evaluate(
        candidate,
        record,
    )
    store.save_decision(decision)
    with pytest.raises(
        FileExistsError,
        match="user-managed",
    ):
        PromotionManager(
            repo,
            store,
        ).promote(
            candidate,
            decision,
        )


def test_promotion_and_rollback_reject_path_traversal(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    with pytest.raises(
        ValueError,
        match="invalid project Skill name",
    ):
        PromotionManager(
            repo,
            store,
        ).rollback(
            "../escape",
            1,
        )


def test_managed_skill_upgrade_requires_parent_hash_and_supports_rollback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    first = _candidate(name="managed-fix")
    first_record = _record(first)
    first_decision = _approve(
        store,
        first,
        first_record,
    )
    manager = PromotionManager(repo, store)
    manager.promote(
        first,
        first_decision,
    )

    second = SkillCandidate.build(
        skill_name=first.skill_name,
        description=first.description,
        instructions=(
            first.instructions
            + "\nRun final verification after the last edit."
        ),
        pattern=_success_pattern(),
        parent_skill_name=first.skill_name,
        parent_skill_version=1,
        parent_skill_hash=first.content_hash,
    )
    second_record = _record(second)
    second_decision = _approve(
        store,
        second,
        second_record,
    )
    manager.promote(
        second,
        second_decision,
    )
    current = SkillCatalog.discover(repo).get(
        first.skill_name
    )
    assert current is not None
    assert "Run final verification" in current.instructions

    manager.rollback(
        first.skill_name,
        1,
    )
    rolled_back = SkillCatalog.discover(repo).get(
        first.skill_name
    )
    assert rolled_back is not None
    assert (
        "Run final verification after the last edit."
        not in rolled_back.instructions
    )


def test_evolution_audit_is_bounded_metadata_and_correlates_candidate_eval_promotion(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = CandidateStore(repo)
    candidate = _candidate()
    record = _record(candidate)
    decision = _approve(
        store,
        candidate,
        record,
    )
    PromotionManager(
        repo,
        store,
    ).promote(
        candidate,
        decision,
    )
    events = [
        json.loads(line)
        for line in (
            store.root / "evolution_events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    event_types = {
        event["event_type"] for event in events
    }
    assert {
        "skill_candidate_created",
        "skill_candidate_evaluated",
        "skill_candidate_approved",
        "skill_candidate_promoted",
    } <= event_types
    raw = json.dumps(events)
    assert candidate.candidate_id in raw
    assert record.record_id in raw
    assert decision.decision_id in raw
    assert candidate.instructions not in raw
    assert "secret" not in raw.lower()
    assert all(
        len(json.dumps(event).encode("utf-8"))
        < 16 * 1024
        for event in events
    )


def test_package_discovery_includes_experience_package():
    pyproject = (
        ROOT / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert '"experience*"' in pyproject
