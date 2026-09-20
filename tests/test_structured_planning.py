from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent.core import Agent, AgentConfig, PrepareNextTurnResult
from agent.runner import ExecutionRunner, RunRequest
from agent.event_log import EventLog
from agent.planning import ExecutionPlan, PlanStepStatus, PlanningRuntime, decide_planning
from agent.prompt import build_task_prompt
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from context.history import ConversationHistory
from evals.coding_agent.__main__ import _planning_mode_for_variant
from evals.coding_agent.graders import extract_metrics
from llm.base import LLMMessage, MockBackend
from tools.base import ToolEffect, ToolRegistry
from tools.file_tool import FileReadTool, FileWriteTool
from tools.shell_tool import ShellTool


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "value.txt").write_text("todo\n", encoding="utf-8")
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "planning-test@example.invalid"),
        ("git", "config", "user.name", "Planning Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "baseline"),
    )
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    return root


def _plan_action(goal: str = "Update value deterministically") -> Action:
    return Action(
        ActionType.TOOL_CALL,
        "create plan",
        ToolCall(
            "plan_create",
            {
                "goal": goal,
                "steps": [
                    {
                        "id": "edit",
                        "description": "Update value.txt",
                        "targets": ["value.txt"],
                        "verification": "Read value.txt",
                    }
                ],
            },
        ),
    )


def _history(task: Task) -> ConversationHistory:
    history = ConversationHistory(max_messages=40)
    history.add(
        LLMMessage(
            role="user",
            content=build_task_prompt(task.description, task.repo_path, task.issue_url),
        )
    )
    return history


def _run_agent(
    tmp_path: Path,
    script: list[Action],
    *,
    planning_mode: str,
    task: Task | None = None,
    prepare_next_turn=None,
):
    repo = Path(task.repo_path) if task is not None else _init_repo(tmp_path / "repo")
    task = task or Task("Inspect the repository.", str(repo), task_id="planning-test")
    history = _history(task)
    backend = MockBackend(script, input_tokens=10, output_tokens=5)
    registry = (
        ToolRegistry()
        .register(FileReadTool(workspace=repo))
        .register(FileWriteTool(workspace=repo))
    )
    agent = Agent(
        backend,
        registry,
        AgentConfig(
            max_steps=max(6, len(script) + 2),
            budget_tokens=20_000,
            repo_map_mode="none",
            planning_mode=planning_mode,
            prepare_next_turn=prepare_next_turn,
        ),
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    try:
        result = agent.run(task, log, history=history)
        trace_path = str(log.path)
    finally:
        log.close()
    return result, agent, backend, history, Path(trace_path)


def _event_rows(trace: Path) -> list[dict]:
    return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line]


def test_planning_schema_validation_and_stable_step_order():
    with pytest.raises(ValueError, match="goal cannot be empty"):
        ExecutionPlan.from_payload({"goal": "", "steps": [{"id": "a", "description": "x"}]})
    with pytest.raises(ValueError, match="at least one step"):
        ExecutionPlan.from_payload({"goal": "g", "steps": []})
    with pytest.raises(ValueError, match="unique"):
        ExecutionPlan.from_payload(
            {
                "goal": "g",
                "steps": [
                    {"id": "same", "description": "first"},
                    {"id": "same", "description": "second"},
                ],
            }
        )

    plan = ExecutionPlan.from_payload(
        {
            "goal": "g",
            "steps": [
                {"id": "inspect", "description": "inspect"},
                {"id": "edit", "description": "edit"},
                {"id": "verify", "description": "verify"},
            ],
        }
    )
    assert [step.step_id for step in plan.steps] == ["inspect", "edit", "verify"]
    assert plan.current_step_id == "inspect"


def test_planning_mode_off_preserves_baseline_no_extra_call(tmp_path: Path):
    result, agent, backend, _, trace = _run_agent(
        tmp_path,
        [Action(ActionType.FINISH, "done", message="done")],
        planning_mode="off",
    )
    assert result.status is RunStatus.SUCCESS
    assert backend.call_count == 1
    assert agent.current_plan is None
    assert "plan_create" not in backend.received_messages[0][0].content
    assert all(row["event_type"] != "plan_created" for row in _event_rows(trace))


def test_auto_decision_is_deterministic_for_simple_and_complex_tasks(tmp_path: Path):
    simple = Task("Fix the typo.", str(tmp_path))
    simple_decision = decide_planning(simple, "auto")
    assert simple_decision.enabled is False
    assert simple_decision.reason == "auto_simple_task"

    tests_required = Task("Fix counter.py.", str(tmp_path), require_tests=True)
    assert decide_planning(tests_required, "auto").reason == "auto_require_tests"

    multi_file = Task("Update formatter.py and service.py.", str(tmp_path))
    decision = decide_planning(multi_file, "auto")
    assert decision.enabled is True
    assert decision.reason == "auto_multiple_file_hints"


def test_auto_simple_task_skips_planning_without_extra_call(tmp_path: Path):
    result, _, backend, _, trace = _run_agent(
        tmp_path,
        [Action(ActionType.FINISH, "done", message="done")],
        planning_mode="auto",
    )
    assert result.status is RunStatus.SUCCESS
    assert backend.call_count == 1
    rows = _event_rows(trace)
    skipped = [row for row in rows if row["event_type"] == "planning_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["payload"]["reason"] == "auto_simple_task"


def test_auto_complex_task_requires_and_can_create_plan(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    task = Task("Update value.txt and tests/test_value.py.", str(repo), task_id="complex")
    result, agent, _, _, trace = _run_agent(
        tmp_path,
        [_plan_action(), Action(ActionType.FINISH, "done", message="done")],
        planning_mode="auto",
        task=task,
    )
    assert result.status is RunStatus.SUCCESS
    assert agent.current_plan is not None
    assert any(row["event_type"] == "plan_created" for row in _event_rows(trace))


def test_always_requires_plan_before_mutation_and_keeps_plan_out_of_history(tmp_path: Path):
    goal = "PLAN-GOAL-ONLY-12345"
    script = [
        Action(
            ActionType.TOOL_CALL,
            "edit too early",
            ToolCall("file_write", {"path": "value.txt", "content": "premature\n"}),
        ),
        _plan_action(goal),
        Action(
            ActionType.TOOL_CALL,
            "edit",
            ToolCall("file_write", {"path": "value.txt", "content": "done\n"}),
        ),
        Action(ActionType.FINISH, "done", message="done"),
    ]
    repo = _init_repo(tmp_path / "repo")
    task = Task(
        "Change value.txt to done.",
        str(repo),
        task_id="mutation-gate",
        require_changes=True,
    )
    result, agent, backend, history, trace = _run_agent(
        tmp_path, script, planning_mode="always", task=task
    )
    assert result.status is RunStatus.SUCCESS
    assert (repo / "value.txt").read_text(encoding="utf-8") == "done\n"
    assert agent.current_plan is not None
    assert any(goal in call[0].content for call in backend.received_messages[2:])
    assert all(goal not in message.content for message in history.to_list())

    event_types = [row["event_type"] for row in _event_rows(trace)]
    assert event_types.index("plan_rejected") < event_types.index("plan_created")


def test_explicit_progress_and_revision_preserve_lineage_and_trace(tmp_path: Path):
    script = [
        _plan_action(),
        Action(
            ActionType.TOOL_CALL,
            "start",
            ToolCall("plan_step_update", {"step_id": "edit", "status": "in_progress"}),
        ),
        Action(
            ActionType.TOOL_CALL,
            "complete",
            ToolCall("plan_step_update", {"step_id": "edit", "status": "completed"}),
        ),
        Action(
            ActionType.TOOL_CALL,
            "revise",
            ToolCall(
                "plan_revise",
                {
                    "reason": "Repository evidence requires verification.",
                    "goal": "Update and verify value",
                    "steps": [
                        {
                            "id": "edit",
                            "description": "Update value.txt",
                            "status": "completed",
                        },
                        {
                            "id": "verify",
                            "description": "Verify final value",
                            "status": "pending",
                        },
                    ],
                },
            ),
        ),
        Action(ActionType.FINISH, "done", message="done"),
    ]
    result, agent, _, _, trace = _run_agent(tmp_path, script, planning_mode="always")
    assert result.status is RunStatus.SUCCESS
    assert agent.current_plan is not None
    assert agent.current_plan.version == 2
    assert agent.current_plan.previous_version == 1
    assert agent.current_plan.revision_reason == "Repository evidence requires verification."
    assert agent.current_plan.steps[0].status is PlanStepStatus.COMPLETED

    event_types = [row["event_type"] for row in _event_rows(trace)]
    for event_type in ("plan_created", "plan_step_started", "plan_step_completed", "plan_revised"):
        assert event_type in event_types


def test_current_plan_survives_prepare_history_override(tmp_path: Path):
    goal = "PLAN-SURVIVES-COMPACTION-12345"

    def compact_like_prepare(context):
        first = context.history.to_list()[0]
        return PrepareNextTurnResult(history_override=(first,))

    result, _, backend, history, _ = _run_agent(
        tmp_path,
        [_plan_action(goal), Action(ActionType.FINISH, "done", message="done")],
        planning_mode="always",
        prepare_next_turn=compact_like_prepare,
    )
    assert result.status is RunStatus.SUCCESS
    assert goal in backend.received_messages[1][0].content
    assert all(goal not in message.content for message in history.to_list())


def test_planning_calls_use_normal_usage_accounting(tmp_path: Path):
    result, _, backend, _, trace = _run_agent(
        tmp_path,
        [_plan_action(), Action(ActionType.FINISH, "done", message="done")],
        planning_mode="always",
    )
    assert result.status is RunStatus.SUCCESS
    assert backend.call_count == 2
    assert result.usage.llm_calls == 2
    assert result.total_tokens == 30

    llm_finished = [row for row in _event_rows(trace) if row["event_type"] == "llm_call_finished"]
    assert len(llm_finished) == 2
    for row in llm_finished:
        breakdown = row["payload"]["token_breakdown"]
        assert breakdown["planning_tokens"] > 0
        assert breakdown["estimated_input_tokens"] == (
            breakdown["system_tokens"]
            + breakdown["repo_map_tokens"]
            + breakdown["planning_tokens"]
            + breakdown["tool_schema_tokens"]
            + breakdown["context_tokens"]
        )


def test_malformed_plan_is_rejected_then_can_be_corrected(tmp_path: Path):
    invalid = Action(
        ActionType.TOOL_CALL,
        "bad plan",
        ToolCall("plan_create", {"goal": "g", "steps": []}),
    )
    result, agent, _, _, trace = _run_agent(
        tmp_path,
        [invalid, _plan_action(), Action(ActionType.FINISH, "done", message="done")],
        planning_mode="always",
    )
    assert result.status is RunStatus.SUCCESS
    assert agent.current_plan is not None
    event_types = [row["event_type"] for row in _event_rows(trace)]
    assert "plan_rejected" in event_types
    assert "plan_created" in event_types


def test_plan_completion_never_bypasses_existing_completion_guard(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    task = Task(
        "Change value.txt.",
        str(repo),
        task_id="completion-authority",
        require_changes=True,
    )
    script = [
        _plan_action(),
        Action(
            ActionType.TOOL_CALL,
            "mark complete",
            ToolCall("plan_step_update", {"step_id": "edit", "status": "completed"}),
        ),
        Action(ActionType.FINISH, "claim done", message="done"),
        Action(ActionType.GIVE_UP, "cannot satisfy guard", message="stop"),
    ]
    result, _, _, _, trace = _run_agent(
        tmp_path, script, planning_mode="always", task=task
    )
    assert result.status is RunStatus.GAVE_UP
    assert any(row["event_type"] == "completion_rejected" for row in _event_rows(trace))


def test_agent_failure_keeps_plan_trace_artifact(tmp_path: Path):
    result, agent, _, _, trace = _run_agent(
        tmp_path,
        [_plan_action(), Action(ActionType.GIVE_UP, "stop", message="cannot continue")],
        planning_mode="always",
    )
    assert result.status is RunStatus.GAVE_UP
    assert agent.current_plan is not None
    assert any(row["event_type"] == "plan_created" for row in _event_rows(trace))


def test_tool_effect_defaults_fail_safe_and_read_tools_opt_in(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    registry = ToolRegistry().register(FileReadTool(workspace=repo)).register(
        FileWriteTool(workspace=repo)
    )
    assert FileReadTool(workspace=repo).effect is ToolEffect.READ_ONLY
    assert FileWriteTool(workspace=repo).effect is ToolEffect.MAY_MUTATE_REPOSITORY
    assert registry.is_mutating("file_read", {"path": "value.txt"}) is False
    assert registry.is_mutating("file_write", {"path": "value.txt"}) is True
    assert registry.is_mutating("future_unknown_tool", {}) is True

    registry.register(ShellTool())
    assert registry.is_mutating(
        "shell",
        {"cmd": "cat pytest.ini; echo ---; cat calculator.py"},
    ) is False
    assert registry.is_mutating(
        "shell",
        {"cmd": "cd /tmp/repo && cat pytest.ini calculator.py"},
    ) is False
    assert registry.is_mutating(
        "shell",
        {"cmd": "cd /tmp/repo && git status --short"},
    ) is False
    assert registry.is_mutating(
        "shell",
        {"cmd": "cd /tmp/repo && echo fixed > value.txt"},
    ) is True
    assert registry.is_mutating(
        "shell",
        {"cmd": "cd /tmp/repo && cat value.txt | tail -1"},
    ) is True
    assert registry.is_mutating("shell", {"cmd": "pytest -q"}) is False
    assert registry.is_mutating("shell", {"cmd": "git status --short"}) is False
    assert registry.is_mutating("shell", {"cmd": "echo fixed > value.txt"}) is True
    assert registry.is_mutating("shell", {"cmd": "git commit -m fix"}) is True
    assert registry.is_mutating("shell", {"cmd": "cat value.txt; rm value.txt"}) is True


def test_eval_architecture_variants_map_to_real_planning_configs():
    assert _planning_mode_for_variant("baseline_react") == "off"
    assert _planning_mode_for_variant("planning") == "always"
    with pytest.raises(ValueError, match="unsupported coding-agent architecture variant"):
        _planning_mode_for_variant("metadata-only")


def test_trial_metrics_extract_planning_lifecycle(tmp_path: Path):
    result, _, _, _, trace = _run_agent(
        tmp_path,
        [
            _plan_action(),
            Action(
                ActionType.TOOL_CALL,
                "done",
                ToolCall("plan_step_update", {"step_id": "edit", "status": "completed"}),
            ),
            Action(
                ActionType.TOOL_CALL,
                "revise",
                ToolCall(
                    "plan_revise",
                    {
                        "reason": "add verification",
                        "goal": "verify",
                        "steps": [{"id": "verify", "description": "verify"}],
                    },
                ),
            ),
            Action(ActionType.FINISH, "done", message="done"),
        ],
        planning_mode="always",
    )
    assert result.status is RunStatus.SUCCESS
    result.trace_path = str(trace)
    metrics = extract_metrics(result, wall_time_seconds=0.1)
    assert metrics.plan_created_count == 1
    assert metrics.plan_revision_count == 1
    assert metrics.plan_step_completed_count == 1
    assert metrics.planning_skipped is False


def test_fake_planning_metrics_are_not_reported_as_real_model_capability():
    from evals.coding_agent.report import build_report
    from evals.coding_agent.schema import GraderResult, TrialMetrics, TrialResult

    row = TrialResult(
        suite_id="suite",
        task_id="task",
        trial_id="task--planning--r001",
        variant="planning",
        repetition=1,
        execution_status="executed",
        evidence_kind="deterministic_harness",
        real_model_executed=False,
        run_status="success",
        termination_reason="completion_satisfied",
        acceptance_status="passed",
        success=True,
        metrics=TrialMetrics(plan_created_count=1, plan_step_completed_count=1),
        grader_results=(GraderResult("g", "file", True, True, "fixture"),),
    )
    report = build_report([row], suite_id="suite")
    assert report["real_model_executed"] is False
    assert report["harness_validation"]["pass_rate_intentionally_omitted"] is True
    assert "real_model_small_sample" not in report


def test_shared_history_preflight_never_sees_previous_run_plan(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    observed_systems: list[str] = []

    def observe_prepare(context):
        observed_systems.append(context.system_content)
        return None

    backend = MockBackend(
        [
            _plan_action("OLD-RUN-PLAN-DO-NOT-LEAK"),
            Action(ActionType.FINISH, "done", message="done"),
            _plan_action("NEW-RUN-PLAN"),
            Action(ActionType.FINISH, "done", message="done"),
        ]
    )
    registry = ToolRegistry().register(FileReadTool(workspace=repo)).register(
        FileWriteTool(workspace=repo)
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=AgentConfig(
            max_steps=6,
            budget_tokens=20_000,
            repo_map_mode="none",
            planning_mode="always",
            prepare_next_turn=observe_prepare,
        ),
        log_dir=str(tmp_path / "runner-logs"),
    )
    history = ConversationHistory(max_messages=40)
    history.add(LLMMessage(role="user", content="Shared task context."))
    history.add(LLMMessage(role="assistant", content="Prior round context."))

    first = Task("First task.", str(repo), task_id="shared-plan-1")
    second = Task("Second task.", str(repo), task_id="shared-plan-2")
    assert runner.run(RunRequest(task=first, history=history)).status is RunStatus.SUCCESS
    before_second = len(observed_systems)
    assert runner.run(RunRequest(task=second, history=history)).status is RunStatus.SUCCESS

    second_preflight = observed_systems[before_second]
    assert "OLD-RUN-PLAN-DO-NOT-LEAK" not in second_preflight


def test_empty_planning_context_has_zero_diagnostic_tokens(tmp_path: Path):
    result, _, _, _, trace = _run_agent(
        tmp_path,
        [Action(ActionType.FINISH, "done", message="done")],
        planning_mode="off",
    )
    assert result.status is RunStatus.SUCCESS
    llm_finished = [
        row for row in _event_rows(trace) if row["event_type"] == "llm_call_finished"
    ]
    assert len(llm_finished) == 1
    assert llm_finished[0]["payload"]["token_breakdown"]["planning_tokens"] == 0


def test_planning_context_exposes_minimal_plan_create_contract(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo-contract")
    task = Task("Inspect the repository.", str(repo), task_id="planning-contract")
    runtime = PlanningRuntime(decide_planning(task, "always"))

    context = runtime.render_context()

    assert "plan_create requires a non-empty goal" in context
    assert "each step requires a non-empty id and description" in context


def test_invalid_plan_create_feedback_repeats_required_shape(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo-invalid-plan")
    task = Task("Inspect the repository.", str(repo), task_id="planning-invalid")
    runtime = PlanningRuntime(decide_planning(task, "always"))

    result = runtime.apply_control("plan_create", {"goal": "", "steps": []})

    assert result.accepted is False
    assert "Expected plan_create params" in result.message
    assert "non-empty id and description" in result.message


def test_system_prompt_does_not_delegate_commit_policy_to_model():
    from agent.prompt import build_system_prompt

    prompt = build_system_prompt(repo_path="/repo", tools=[])

    assert "Do not create a Git commit unless" not in prompt
    assert "git_status/git_diff/git_add/git_commit" not in prompt

def test_plan_step_update_feedback_lists_non_terminal_steps(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo-update-contract")
    task = Task("Inspect the repository.", str(repo), task_id="planning-update-contract")
    runtime = PlanningRuntime(decide_planning(task, "always"))
    assert runtime.apply_control(
        "plan_create",
        {
            "goal": "inspect then verify",
            "steps": [
                {"id": "inspect", "description": "inspect"},
                {"id": "verify", "description": "verify"},
            ],
        },
    ).accepted
    assert runtime.apply_control(
        "plan_step_update",
        {"step_id": "inspect", "status": "completed"},
    ).accepted

    rejected = runtime.apply_control(
        "plan_step_update",
        {"step_id": "inspect", "status": "completed"},
    )

    assert rejected.accepted is False
    assert "Current non-terminal step ids: verify" in rejected.message
