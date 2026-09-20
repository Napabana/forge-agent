from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent.core import Agent, AgentConfig, PrepareNextTurnResult
from agent.event_log import EventLog
from agent.recovery import (
    FailureCategory,
    FailureContext,
    FailureSource,
    RecoveryPolicy,
    RecoveryStrategy,
    SemanticProgressTracker,
)
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from evals.coding_agent.__main__ import (
    _planning_mode_for_variant,
    _recovery_mode_for_variant,
)
from llm.base import MockBackend
from tools.base import (
    BaseTool,
    FailingTool,
    ToolEffect,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
)
from tools.file_tool import FileReadTool, FileWriteTool


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "value.txt").write_text("old\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "recovery-test@example.invalid"),
        ("git", "config", "user.name", "Recovery Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "baseline"),
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    return root


def _call(name: str, params: dict | None = None) -> Action:
    return Action(ActionType.TOOL_CALL, f"call {name}", ToolCall(name, params or {}))


def _finish() -> Action:
    return Action(ActionType.FINISH, "done", message="done")


def _plan_action() -> Action:
    return Action(
        ActionType.TOOL_CALL,
        "create plan",
        ToolCall(
            "plan_create",
            {
                "goal": "Update and verify value.txt",
                "steps": [
                    {
                        "id": "edit",
                        "description": "Update value.txt",
                        "targets": ["value.txt"],
                    },
                    {
                        "id": "verify",
                        "description": "Run verification",
                        "verification": "Run test",
                    },
                ],
            },
        ),
    )


def _revise_action() -> Action:
    return Action(
        ActionType.TOOL_CALL,
        "revise plan after repeated failure",
        ToolCall(
            "plan_revise",
            {
                "reason": "Repeated verification failure requires a different approach.",
                "goal": "Update value.txt using the corrected approach and verify it",
                "steps": [
                    {
                        "id": "edit",
                        "description": "Apply corrected update to value.txt",
                        "targets": ["value.txt"],
                        "status": "pending",
                    },
                    {
                        "id": "verify",
                        "description": "Run verification after the update",
                        "verification": "Run test",
                        "status": "pending",
                    },
                ],
            },
        ),
    )


class SequenceTestTool(BaseTool):
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.call_count = 0

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect.READ_ONLY

    @property
    def name(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return "deterministic sequential test tool"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict) -> ToolResult:
        self.call_count += 1
        if not self.outcomes:
            raise AssertionError("test outcome script exhausted")
        passed = self.outcomes.pop(0)
        if passed:
            return ToolResult(success=True, output="1 passed")
        return ToolResult(
            success=False,
            output="1 failed",
            error="AssertionError: expected corrected value",
            error_type=ToolErrorType.TOOL_EXECUTION,
        )


def _run(
    tmp_path: Path,
    script: list[Action],
    registry: ToolRegistry,
    *,
    task: Task | None = None,
    config: AgentConfig | None = None,
):
    repo = Path(task.repo_path) if task is not None else _init_repo(tmp_path / "repo")
    task = task or Task("Exercise structured recovery.", str(repo), task_id="recovery-test")
    backend = MockBackend(script)
    agent = Agent(
        backend,
        registry,
        config
        or AgentConfig(
            max_steps=max(8, len(script) + 1),
            repo_map_mode="none",
            recovery_mode="structured",
        ),
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    try:
        result = agent.run(task, log)
        trace = Path(log.path)
    finally:
        log.close()
    rows = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return result, agent, backend, rows


def _events(rows: list[dict], event_type: str) -> list[dict]:
    return [row for row in rows if row["event_type"] == event_type]


def test_policy_is_deterministic_and_bounded():
    policy = RecoveryPolicy("structured", max_attempts=2)
    base = dict(
        category=FailureCategory.TEST_FAILURE,
        source=FailureSource.TEST,
        step=1,
        evidence="assertion failed",
        plan_version=1,
        plan_step_id="verify",
    )

    first = policy.decide(FailureContext(**base))
    assert first is not None
    assert first.strategy is RecoveryStrategy.INSPECT
    assert first.requires_plan_revision is False

    second = policy.decide(FailureContext(**{**base, "step": 2}))
    assert second is not None
    assert second.strategy is RecoveryStrategy.REPLAN
    assert second.requires_plan_revision is True

    exhausted = policy.decide(FailureContext(**{**base, "step": 3}))
    assert exhausted is not None
    assert exhausted.strategy is RecoveryStrategy.GIVE_UP
    assert exhausted.terminal is True
    assert exhausted.budget_exhausted is True


def test_permission_denied_changes_approach_instead_of_retrying():
    decision = RecoveryPolicy("structured").decide(
        FailureContext(
            category=FailureCategory.PERMISSION_DENIED,
            source=FailureSource.TOOL,
            step=1,
            evidence="policy denied",
            error_type="permission_denied",
        )
    )
    assert decision is not None
    assert decision.strategy is RecoveryStrategy.CHANGE_APPROACH


def test_recovery_off_preserves_legacy_test_reflection(tmp_path: Path):
    registry = ToolRegistry().register(FailingTool("test", "1 failed"))
    result, _, _, rows = _run(
        tmp_path,
        [_call("test"), Action(ActionType.GIVE_UP, "stop", message="stop")],
        registry,
        config=AgentConfig(
            max_steps=3,
            repo_map_mode="none",
            recovery_mode="off",
        ),
    )
    assert result.status is RunStatus.GAVE_UP
    assert len(_events(rows, EventType.REFLECTION.value)) == 1
    assert _events(rows, EventType.FAILURE_CLASSIFIED.value) == []
    assert _events(rows, EventType.RECOVERY_SELECTED.value) == []


def test_repeated_test_failure_requires_real_plan_revision_before_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    test_tool = SequenceTestTool([False, False, True])
    registry = (
        ToolRegistry()
        .register(test_tool)
        .register(FileWriteTool(workspace=repo))
    )
    task = Task(
        "Change value.txt and verify the final state.",
        str(repo),
        task_id="replan-gate",
        require_changes=True,
        require_tests=True,
        max_steps=10,
    )
    script = [
        _plan_action(),
        _call("test"),
        _call("test"),
        _call("file_write", {"path": "value.txt", "content": "blocked\n"}),
        _revise_action(),
        _call("file_write", {"path": "value.txt", "content": "new\n"}),
        _call("test"),
        _finish(),
    ]
    result, agent, backend, rows = _run(
        tmp_path,
        script,
        registry,
        task=task,
        config=AgentConfig(
            max_steps=10,
            repo_map_mode="none",
            planning_mode="always",
            recovery_mode="structured",
            recovery_max_attempts=4,
            prepare_next_turn=lambda context: PrepareNextTurnResult(
                history_override=(context.history.to_list()[0],)
            ),
        ),
    )

    assert result.status is RunStatus.SUCCESS
    assert agent.current_plan is not None and agent.current_plan.version == 2
    assert (repo / "value.txt").read_text(encoding="utf-8") == "new\n"
    assert test_tool.call_count == 3
    # The recovery gate is runtime state, not just a history prompt. It remains
    # visible even though prepare_next_turn replaces visible history each turn.
    assert "Recovery requires a plan revision newer than v1" in backend.received_messages[3][0].content

    recoveries = _events(rows, EventType.RECOVERY_SELECTED.value)
    assert [row["payload"]["strategy"] for row in recoveries] == ["inspect", "replan"]
    assert recoveries[1]["payload"]["requires_plan_revision"] is True
    blocked = _events(rows, EventType.RECOVERY_BLOCKED.value)
    assert len(blocked) == 1
    assert blocked[0]["payload"]["blocked_action"] == "file_write"
    assert len(_events(rows, EventType.PLAN_REVISED.value)) == 1


def test_completion_rejection_selects_rerun_test_without_forcing_replan(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    test_tool = SequenceTestTool([True])
    registry = ToolRegistry().register(test_tool)
    task = Task(
        "Verify the repository.",
        str(repo),
        task_id="completion-recovery",
        require_tests=True,
        max_steps=6,
    )
    result, _, _, rows = _run(
        tmp_path,
        [_plan_action(), _finish(), _call("test"), _finish()],
        registry,
        task=task,
        config=AgentConfig(
            max_steps=6,
            repo_map_mode="none",
            planning_mode="always",
            recovery_mode="structured",
        ),
    )
    assert result.status is RunStatus.SUCCESS
    recoveries = _events(rows, EventType.RECOVERY_SELECTED.value)
    assert len(recoveries) == 1
    assert recoveries[0]["payload"]["category"] == "completion_rejected"
    assert recoveries[0]["payload"]["strategy"] == "rerun_test"
    assert recoveries[0]["payload"]["requires_plan_revision"] is False


def test_recovery_budget_exhaustion_stops_as_incomplete(tmp_path: Path):
    registry = ToolRegistry().register(FailingTool("work", "deterministic failure"))
    result, _, backend, rows = _run(
        tmp_path,
        [_call("work"), _call("work"), _finish()],
        registry,
        config=AgentConfig(
            max_steps=4,
            repo_map_mode="none",
            recovery_mode="structured",
            recovery_max_attempts=1,
            reflection_no_edit_steps=100,
        ),
    )
    assert result.status is RunStatus.INCOMPLETE
    assert result.termination_reason == "recovery_exhausted"
    assert result.resource_reason == "recovery_budget"
    assert backend.call_count == 2
    assert len(_events(rows, EventType.FAILURE_CLASSIFIED.value)) == 2
    assert len(_events(rows, EventType.RECOVERY_SELECTED.value)) == 1
    assert len(_events(rows, EventType.RECOVERY_EXHAUSTED.value)) == 1


def test_infrastructure_failure_keeps_existing_fatal_contract(tmp_path: Path):
    registry = ToolRegistry().register(
        FailingTool("shell", "Failed to start container: Docker is not available")
    )
    result, _, _, rows = _run(
        tmp_path,
        [_call("shell"), _finish()],
        registry,
        config=AgentConfig(
            max_steps=3,
            repo_map_mode="none",
            recovery_mode="structured",
            fatal_tool_error_repeats=1,
        ),
    )
    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "infrastructure_error"
    assert _events(rows, EventType.FAILURE_CLASSIFIED.value) == []
    assert _events(rows, EventType.RECOVERY_SELECTED.value) == []


def test_eval_variant_maps_to_real_planning_and_recovery_configs():
    assert _planning_mode_for_variant("baseline_react") == "off"
    assert _recovery_mode_for_variant("baseline_react") == "off"
    assert _planning_mode_for_variant("planning") == "always"
    assert _recovery_mode_for_variant("planning") == "off"
    assert _planning_mode_for_variant("planning_recovery") == "always"
    assert _recovery_mode_for_variant("planning_recovery") == "structured"


def test_verified_current_change_does_not_trigger_no_progress_replan(tmp_path: Path):
    repo = _init_repo(tmp_path / "verified-progress")
    registry = (
        ToolRegistry()
        .register(FileWriteTool(workspace=repo))
        .register(SequenceTestTool([True]))
        .register(FileReadTool(workspace=repo))
    )
    task = Task(
        "Change value.txt and verify it.",
        str(repo),
        task_id="verified-progress",
        require_changes=True,
        require_tests=True,
        max_steps=8,
    )
    script = [
        _plan_action(),
        _call("file_write", {"path": "value.txt", "content": "done\n"}),
        _call("test"),
        _call("file_read", {"path": "value.txt"}),
        _call("file_read", {"path": "value.txt"}),
        _finish(),
    ]

    result, _, _, rows = _run(
        tmp_path,
        script,
        registry,
        task=task,
        config=AgentConfig(
            max_steps=8,
            repo_map_mode="none",
            planning_mode="always",
            recovery_mode="structured",
            reflection_no_edit_steps=2,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    no_progress = [
        row for row in _events(rows, EventType.FAILURE_CLASSIFIED.value)
        if row["payload"]["category"] == "no_progress"
    ]
    assert no_progress == []


def test_pending_replan_does_not_override_completion_guard(tmp_path: Path):
    repo = _init_repo(tmp_path / "finish-with-pending-replan")
    test_tool = SequenceTestTool([False, False, True])
    registry = (
        ToolRegistry()
        .register(FileWriteTool(workspace=repo))
        .register(test_tool)
    )
    task = Task(
        "Change value.txt and verify the final state.",
        str(repo),
        task_id="finish-with-pending-replan",
        require_changes=True,
        require_tests=True,
        max_steps=8,
    )
    script = [
        _plan_action(),
        _call("file_write", {"path": "value.txt", "content": "done\n"}),
        _call("test"),
        _call("test"),
        _call("test"),
        _finish(),
    ]

    result, agent, _, rows = _run(
        tmp_path,
        script,
        registry,
        task=task,
        config=AgentConfig(
            max_steps=8,
            repo_map_mode="none",
            planning_mode="always",
            recovery_mode="structured",
            recovery_max_attempts=4,
            reflection_no_edit_steps=100,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    assert agent.current_plan is not None and agent.current_plan.version == 1
    recoveries = _events(rows, EventType.RECOVERY_SELECTED.value)
    assert [row["payload"]["strategy"] for row in recoveries] == ["inspect", "replan"]
    assert recoveries[-1]["payload"]["requires_plan_revision"] is True
    blocked = _events(rows, EventType.RECOVERY_BLOCKED.value)
    assert all(row["payload"]["blocked_action"] != "finish" for row in blocked)



def test_unrelated_failure_categories_do_not_share_small_category_budget():
    policy = RecoveryPolicy("structured", max_attempts=2)
    categories = (
        (FailureCategory.TOOL_FAILURE, FailureSource.TOOL),
        (FailureCategory.NO_PROGRESS, FailureSource.PROGRESS_GUARD),
        (FailureCategory.TEST_FAILURE, FailureSource.TEST),
    )
    decisions = [
        policy.decide(
            FailureContext(
                category=category,
                source=source,
                step=index,
                evidence=f"evidence-{index}",
            )
        )
        for index, (category, source) in enumerate(categories, start=1)
    ]

    assert all(decision is not None and not decision.terminal for decision in decisions)
    assert [decision.category_occurrence for decision in decisions] == [1, 1, 1]
    assert [decision.global_attempt for decision in decisions] == [1, 2, 3]
    assert decisions[-1].global_max_attempts == 6


def test_same_failure_category_still_exhausts_its_own_budget():
    policy = RecoveryPolicy("structured", max_attempts=2)
    context = FailureContext(
        category=FailureCategory.TEST_FAILURE,
        source=FailureSource.TEST,
        step=1,
        evidence="same failing test",
    )

    assert policy.decide(context).budget_exhausted is False
    assert policy.decide(context).budget_exhausted is False
    exhausted = policy.decide(context)

    assert exhausted.budget_exhausted is True
    assert exhausted.category_occurrence == 2
    assert exhausted.global_attempt == 2


def test_global_recovery_hard_ceiling_still_bounds_distinct_categories():
    policy = RecoveryPolicy("structured", max_attempts=2, global_max_attempts=3)
    contexts = [
        FailureContext(FailureCategory.TOOL_FAILURE, FailureSource.TOOL, 1, "tool"),
        FailureContext(FailureCategory.NO_PROGRESS, FailureSource.PROGRESS_GUARD, 2, "progress"),
        FailureContext(FailureCategory.TEST_FAILURE, FailureSource.TEST, 3, "test"),
        FailureContext(FailureCategory.PERMISSION_DENIED, FailureSource.TOOL, 4, "permission"),
    ]

    first_three = [policy.decide(context) for context in contexts[:3]]
    exhausted = policy.decide(contexts[3])

    assert all(decision is not None and not decision.terminal for decision in first_three)
    assert exhausted.terminal is True
    assert exhausted.budget_exhausted is True
    assert exhausted.global_attempt == 3
    assert exhausted.global_max_attempts == 3
    assert exhausted.to_payload()["category_max_attempts"] == 2


def test_semantic_progress_tracker_deduplicates_bounded_evidence():
    tracker = SemanticProgressTracker(capacity=2)

    assert tracker.mark_once("skill", "bug-fix") is True
    assert tracker.mark_once("skill", "bug-fix") is False
    assert tracker.mark_once("plan", "plan_created:1") is True
    assert tracker.mark_once("plan", "plan_created:1") is False
    assert tracker.mark_once("test", "failed:a") is True
    assert tracker.mark_once("test", "failed:a") is False
    assert tracker.mark_once("test", "passed:a") is True
    assert tracker.mark_once("test", "failed:b") is True


def test_plan_created_resets_no_progress_counter(tmp_path: Path):
    repo = _init_repo(tmp_path / "plan-progress")
    registry = ToolRegistry().register(FileReadTool(workspace=repo))
    task = Task("Inspect then plan.", str(repo), task_id="plan-progress", max_steps=6)
    result, _, _, rows = _run(
        tmp_path,
        [
            _call("file_read", {"path": "value.txt"}),
            _plan_action(),
            _call("file_read", {"path": "value.txt"}),
            _finish(),
        ],
        registry,
        task=task,
        config=AgentConfig(
            max_steps=6,
            repo_map_mode="none",
            planning_mode="always",
            recovery_mode="structured",
            reflection_no_edit_steps=2,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    no_progress = [
        row for row in _events(rows, EventType.FAILURE_CLASSIFIED.value)
        if row["payload"]["category"] == "no_progress"
    ]
    assert no_progress == []


def test_repeated_plain_file_reads_still_trigger_no_progress(tmp_path: Path):
    repo = _init_repo(tmp_path / "read-stagnation")
    registry = ToolRegistry().register(FileReadTool(workspace=repo))
    task = Task("Inspect the repository.", str(repo), task_id="read-stagnation", max_steps=5)
    result, _, _, rows = _run(
        tmp_path,
        [
            _call("file_read", {"path": "value.txt"}),
            _call("file_read", {"path": "value.txt"}),
            _finish(),
        ],
        registry,
        task=task,
        config=AgentConfig(
            max_steps=5,
            repo_map_mode="none",
            recovery_mode="structured",
            reflection_no_edit_steps=2,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    no_progress = [
        row for row in _events(rows, EventType.FAILURE_CLASSIFIED.value)
        if row["payload"]["category"] == "no_progress"
    ]
    assert len(no_progress) == 1


def test_new_test_evidence_progresses_once_but_duplicate_result_does_not(tmp_path: Path):
    repo = _init_repo(tmp_path / "test-evidence")
    registry = ToolRegistry().register(SequenceTestTool([True, True]))
    task = Task("Inspect verification evidence.", str(repo), task_id="test-evidence", max_steps=5)
    result, _, _, rows = _run(
        tmp_path,
        [_call("test"), _call("test"), _finish()],
        registry,
        task=task,
        config=AgentConfig(
            max_steps=5,
            repo_map_mode="none",
            recovery_mode="structured",
            reflection_no_edit_steps=1,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    no_progress = [
        row for row in _events(rows, EventType.FAILURE_CLASSIFIED.value)
        if row["payload"]["category"] == "no_progress"
    ]
    assert len(no_progress) == 1
