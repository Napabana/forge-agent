"""Deterministic, offline failure regression matrix for P1-4.

This suite deliberately drives the production ExecutionRunner -> Agent -> ToolExecutor
lifecycle. Fakes only inject provider/tool/hook/permission/cancel failures; they do
not implement a second agent loop and never access the network or a paid provider.
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Callable

import pytest

from agent.core import AgentConfig
from agent.event_log import EventLog
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import (
    Action,
    ActionType,
    EventType,
    ObservationStatus,
    RunResult,
    RunStatus,
    Task,
    ToolCall,
)
from context.history import ConversationHistory
from entry import github_issue
from harness.hooks import HookBlockResult, HookEvent, Hooks
from harness.permission import Decision, PermissionDecision, PermissionManager
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema
from llm.usage import TokenUsage
from tools.base import BaseTool, FailingTool, NoopTool, ToolErrorType, ToolRegistry, ToolResult


ScriptItem = Action | LLMResponse | BaseException | Callable[["ScriptedFailureBackend"], Action | LLMResponse]


class ScriptedFailureBackend(LLMBackend):
    """Offline provider double that can deterministically return or raise per call."""

    def __init__(self, script: list[ScriptItem]) -> None:
        self._script = deque(script)
        self.call_count = 0
        self.received_messages: list[list[LLMMessage]] = []

    @property
    def model_name(self) -> str:
        return "deterministic-failure-backend"

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        del tools
        self.call_count += 1
        self.received_messages.append(list(messages))
        if not self._script:
            raise AssertionError("failure backend script exhausted")
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(self)
        if isinstance(item, Action):
            return LLMResponse(item, f"[failure-harness] {item.action_type.value}", TokenUsage())
        if isinstance(item, LLMResponse):
            return item
        raise AssertionError(f"unsupported scripted item: {type(item).__name__}")


class CancelOnRetryWait:
    """Event-like object that flips to canceled when retry backoff waits."""

    def __init__(self) -> None:
        self._set = False
        self.wait_calls = 0

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, _timeout: float) -> bool:
        self.wait_calls += 1
        self._set = True
        return True


class FixedPermission(PermissionManager):
    def __init__(
        self,
        decision: PermissionDecision | None = None,
        *,
        on_check: Callable[[], None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._decision = decision or PermissionDecision(Decision.ALLOW)
        self._on_check = on_check
        self._error = error
        self.calls = 0

    def check(self, block):
        del block
        self.calls += 1
        if self._on_check is not None:
            self._on_check()
        if self._error is not None:
            raise self._error
        return self._decision


class RecordingTool(BaseTool):
    def __init__(
        self,
        name: str = "work",
        *,
        result: ToolResult | None = None,
        on_execute: Callable[[], None] | None = None,
        require_value: bool = False,
    ) -> None:
        self._name = name
        self._result = result or ToolResult(success=True, output="ok")
        self._on_execute = on_execute
        self._require_value = require_value
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "deterministic failure harness tool"

    @property
    def parameters_schema(self) -> dict:
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        }
        if self._require_value:
            schema["required"] = ["value"]
        return schema

    def execute(self, params: dict) -> ToolResult:
        del params
        self.calls += 1
        if self._on_execute is not None:
            self._on_execute()
        return self._result


class RaisingTool(RecordingTool):
    def execute(self, params: dict) -> ToolResult:
        del params
        self.calls += 1
        raise ValueError("tool exploded")


def finish(message: str = "done") -> Action:
    return Action(ActionType.FINISH, "finish", message=message)


def give_up() -> Action:
    return Action(ActionType.GIVE_UP, "cannot continue", message="gave up")


def call(name: str = "work", params: dict | None = None) -> Action:
    return Action(ActionType.TOOL_CALL, f"call {name}", ToolCall(name, params or {}))


def reflect() -> Action:
    return Action(ActionType.REFLECTION, "reconsider")


def make_task(tmp_path: Path, *, max_steps: int = 4, task_id: str = "failure") -> Task:
    repo = tmp_path / task_id
    repo.mkdir(parents=True, exist_ok=True)
    return Task(
        description="Inspect deterministic failure semantics.",
        repo_path=str(repo),
        task_id=task_id,
        max_steps=max_steps,
    )


def make_runner(
    tmp_path: Path,
    backend: LLMBackend,
    registry: ToolRegistry | None = None,
    *,
    config: AgentConfig | None = None,
    confirm_callback=None,
) -> ExecutionRunner:
    return ExecutionRunner(
        backend=backend,
        registry=registry or ToolRegistry(),
        config=config or AgentConfig(),
        confirm_callback=confirm_callback,
        log_dir=str(tmp_path / "logs"),
    )


def read_events(result: RunResult):
    assert result.trace_path
    with EventLog.open_existing(result.trace_path) as log:
        return log.replay()


def event_payloads(result: RunResult, event_type: EventType) -> list[dict]:
    return [event.payload for event in read_events(result) if event.event_type is event_type]


def last_observation(result: RunResult) -> dict:
    return event_payloads(result, EventType.OBSERVATION)[-1]["observation"]


def assert_terminated(result: RunResult, status: RunStatus, reason: str) -> None:
    assert result.status is status
    assert result.termination_reason == reason
    termination = event_payloads(result, EventType.RUN_TERMINATED)[-1]
    assert termination["status"] == status.value
    assert termination["termination_reason"] == reason


# Provider failure injection -------------------------------------------------


def test_provider_transient_retry_then_success_is_offline_and_traced(tmp_path):
    backend = ScriptedFailureBackend([ConnectionError("temporarily unavailable"), finish()])
    runner = make_runner(
        tmp_path,
        backend,
        config=AgentConfig(llm_max_retries=2, llm_retry_delay=0),
    )

    result = runner.run(RunRequest(make_task(tmp_path), entrypoint="cli"))

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert backend.call_count == 2
    retries = event_payloads(result, EventType.LLM_CALL_RETRY)
    assert len(retries) == 1
    assert retries[0]["error_type"] == "connection"
    assert len(event_payloads(result, EventType.LLM_CALL_FINISHED)) == 1


def test_provider_retry_exhausted_is_failed_provider_error(tmp_path):
    backend = ScriptedFailureBackend(
        [TimeoutError("provider timeout"), TimeoutError("provider timeout again")]
    )
    runner = make_runner(
        tmp_path,
        backend,
        config=AgentConfig(llm_max_retries=2, llm_retry_delay=0),
    )

    result = runner.run(RunRequest(make_task(tmp_path), entrypoint="api"))

    assert_terminated(result, RunStatus.FAILED, "provider_error")
    assert backend.call_count == 2
    assert len(event_payloads(result, EventType.LLM_CALL_RETRY)) == 1
    failed = event_payloads(result, EventType.LLM_CALL_FAILED)[0]
    assert failed["error_type"] == "timeout"


def test_provider_non_retryable_and_parser_failure_do_not_retry(tmp_path):
    for error in (
        RuntimeError("unknown provider failure"),
        ValueError("malformed provider response"),
    ):
        case = type(error).__name__.lower()
        backend = ScriptedFailureBackend([error])
        runner = make_runner(
            tmp_path,
            backend,
            config=AgentConfig(llm_max_retries=4, llm_retry_delay=0),
        )
        result = runner.run(
            RunRequest(make_task(tmp_path, task_id=f"provider-{case}"), entrypoint="chat")
        )

        assert_terminated(result, RunStatus.FAILED, "provider_error")
        assert backend.call_count == 1
        assert event_payloads(result, EventType.LLM_CALL_RETRY) == []
        assert len(event_payloads(result, EventType.LLM_CALL_FAILED)) == 1


def test_provider_retry_wait_cancel_stops_without_second_provider_call(tmp_path):
    cancel = CancelOnRetryWait()
    backend = ScriptedFailureBackend([ConnectionError("network unavailable"), finish()])
    runner = make_runner(
        tmp_path,
        backend,
        config=AgentConfig(
            llm_max_retries=3,
            llm_retry_delay=0.1,
            llm_retry_max_delay=0.1,
        ),
    )

    result = runner.run(RunRequest(make_task(tmp_path), cancel_event=cancel, entrypoint="api"))

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert backend.call_count == 1
    assert cancel.wait_calls == 1
    assert len(event_payloads(result, EventType.LLM_CALL_RETRY)) == 1
    failed = event_payloads(result, EventType.LLM_CALL_FAILED)[0]
    assert failed["error_type"] == "canceled"


# Hook / permission / tool failure matrix -----------------------------------


def test_pre_hook_block_is_recoverable_and_skips_permission_and_tool(tmp_path):
    tool = RecordingTool()
    permission = FixedPermission()
    hooks = Hooks().register(
        HookEvent.PRE_TOOL_USE,
        lambda _block: HookBlockResult("policy block"),
    )
    backend = ScriptedFailureBackend([call(), finish()])
    result = make_runner(tmp_path, backend, ToolRegistry().register(tool)).run(
        RunRequest(
            make_task(tmp_path),
            hooks=hooks,
            permission=permission,
            entrypoint="cli",
        )
    )

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert tool.calls == 0
    assert permission.calls == 0
    observation = last_observation(result)
    assert observation["status"] == ObservationStatus.ERROR.value
    assert observation["error_type"] == ToolErrorType.HOOK_BLOCKED.value


def test_pre_hook_exception_is_recoverable_hook_failed(tmp_path):
    hooks = Hooks().register(
        HookEvent.PRE_TOOL_USE,
        lambda _block: (_ for _ in ()).throw(ValueError("hook crash")),
    )
    tool = RecordingTool()
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call(), finish()]),
        ToolRegistry().register(tool),
    ).run(RunRequest(make_task(tmp_path), hooks=hooks))

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert tool.calls == 0
    assert last_observation(result)["error_type"] == ToolErrorType.HOOK_FAILED.value


def test_post_hook_exception_preserves_real_tool_result_and_side_effect(tmp_path):
    tool = RecordingTool()
    hooks = Hooks().register(
        HookEvent.POST_TOOL_USE,
        lambda _block, _result: (_ for _ in ()).throw(RuntimeError("post crash")),
    )
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call(), finish()]),
        ToolRegistry().register(tool),
    ).run(RunRequest(make_task(tmp_path), hooks=hooks))

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert tool.calls == 1
    finished_event = event_payloads(result, EventType.TOOL_EXECUTION_FINISHED)[0]
    assert finished_event["diagnostics"] == ["post_tool_hook:RuntimeError"]
    assert last_observation(result)["status"] == ObservationStatus.SUCCESS.value


@pytest.mark.parametrize(
    ("decision", "confirm_callback", "expected_decision"),
    [
        (PermissionDecision(Decision.DENY, "policy denied"), None, "deny"),
        (PermissionDecision(Decision.CONFIRM, "ask user"), lambda _prompt: False, "confirm"),
    ],
)
def test_permission_deny_and_confirm_reject_are_recoverable(
    tmp_path, decision, confirm_callback, expected_decision
):
    tool = RecordingTool()
    permission = FixedPermission(decision)
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call(), finish()]),
        ToolRegistry().register(tool),
        confirm_callback=confirm_callback,
    ).run(RunRequest(make_task(tmp_path), permission=permission))

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert tool.calls == 0
    assert last_observation(result)["error_type"] == ToolErrorType.PERMISSION_DENIED.value
    permission_trace = event_payloads(result, EventType.PERMISSION_DECISION)[0]
    assert permission_trace["decision"] == expected_decision


@pytest.mark.parametrize("phase", ["permission", "confirm"])
def test_permission_subsystem_and_confirm_callback_crashes_are_infrastructure(
    tmp_path, phase
):
    tool = RecordingTool()
    if phase == "permission":
        permission = FixedPermission(error=RuntimeError("permission backend crashed"))
        confirm = None
    else:
        permission = FixedPermission(PermissionDecision(Decision.CONFIRM, "ask"))
        confirm = lambda _prompt: (_ for _ in ()).throw(RuntimeError("confirm crashed"))

    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call()]),
        ToolRegistry().register(tool),
        confirm_callback=confirm,
    ).run(RunRequest(make_task(tmp_path), permission=permission))

    assert_terminated(result, RunStatus.FAILED, "infrastructure_error")
    assert tool.calls == 0
    failed = event_payloads(result, EventType.TOOL_EXECUTION_FAILED)[0]
    assert failed["error_type"] == ToolErrorType.INFRASTRUCTURE.value
    assert failed["lifecycle_phase"] in {"permission", "permission_confirm"}


def test_unknown_tool_invalid_arguments_normal_failure_exception_and_timeout_are_recoverable(tmp_path):
    cases = []
    cases.append((
        "unknown", ToolRegistry(), call("missing"),
        ToolErrorType.UNKNOWN_TOOL.value, ObservationStatus.ERROR.value,
    ))
    required = RecordingTool("required", require_value=True)
    cases.append((
        "invalid", ToolRegistry().register(required), call("required"),
        ToolErrorType.INVALID_ARGUMENTS.value, ObservationStatus.ERROR.value,
    ))
    failed = RecordingTool(
        "failed",
        result=ToolResult(success=False, output="", error="expected failure"),
    )
    cases.append((
        "normal", ToolRegistry().register(failed), call("failed"),
        ToolErrorType.TOOL_EXECUTION.value, ObservationStatus.ERROR.value,
    ))
    raising = RaisingTool("raising")
    cases.append((
        "exception", ToolRegistry().register(raising), call("raising"),
        ToolErrorType.TOOL_EXECUTION.value, ObservationStatus.ERROR.value,
    ))
    timed = RecordingTool(
        "timed",
        result=ToolResult(
            success=False,
            output="",
            error="operation timed out",
            error_type=ToolErrorType.TIMEOUT,
        ),
    )
    cases.append((
        "timeout", ToolRegistry().register(timed), call("timed"),
        ToolErrorType.TIMEOUT.value, ObservationStatus.TIMEOUT.value,
    ))

    for name, registry, action, error_type, observation_status in cases:
        result = make_runner(
            tmp_path,
            ScriptedFailureBackend([action, finish()]),
            registry,
        ).run(RunRequest(make_task(tmp_path, task_id=f"tool-{name}")))

        assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
        observation = last_observation(result)
        assert observation["error_type"] == error_type
        assert observation["status"] == observation_status


def test_runtime_infrastructure_first_failure_then_finish_is_unresolved_fatal(tmp_path):
    error = "Failed to start container: Docker is not available"
    registry = ToolRegistry().register(FailingTool("shell", error))
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call("shell"), finish()]),
        registry,
        config=AgentConfig(fatal_tool_error_repeats=2),
    ).run(RunRequest(make_task(tmp_path)))

    assert_terminated(result, RunStatus.FAILED, "infrastructure_error")
    assert result.steps_taken == 2
    first = last_observation(result)
    assert first["error_type"] == ToolErrorType.INFRASTRUCTURE.value


def test_repeated_fatal_runtime_infrastructure_aborts_before_finish(tmp_path):
    error = "Failed to start container: Duplicate mount point: /workspace"
    backend = ScriptedFailureBackend([call("shell"), call("shell"), finish()])
    registry = ToolRegistry().register(FailingTool("shell", error))
    result = make_runner(
        tmp_path,
        backend,
        registry,
        config=AgentConfig(fatal_tool_error_repeats=2),
    ).run(RunRequest(make_task(tmp_path, max_steps=3)))

    assert_terminated(result, RunStatus.FAILED, "infrastructure_error")
    assert backend.call_count == 2
    assert result.steps_taken == 2


# Cooperative cancellation at safe boundaries ------------------------------


def test_cancel_before_run_calls_no_provider(tmp_path):
    cancel = threading.Event()
    cancel.set()
    backend = ScriptedFailureBackend([finish()])
    result = make_runner(tmp_path, backend).run(
        RunRequest(make_task(tmp_path), cancel_event=cancel)
    )

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert backend.call_count == 0


def test_cancel_after_pre_hook_stops_before_permission_and_tool(tmp_path):
    cancel = threading.Event()
    permission = FixedPermission()
    tool = RecordingTool()
    hooks = Hooks().register(HookEvent.PRE_TOOL_USE, lambda _block: cancel.set())
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call()]),
        ToolRegistry().register(tool),
    ).run(
        RunRequest(
            make_task(tmp_path),
            hooks=hooks,
            permission=permission,
            cancel_event=cancel,
        )
    )

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert permission.calls == 0
    assert tool.calls == 0
    failed = event_payloads(result, EventType.TOOL_EXECUTION_FAILED)[0]
    assert failed["error_type"] == "canceled"
    assert failed["lifecycle_phase"] == "after_pre_hook"


def test_cancel_after_permission_stops_before_tool(tmp_path):
    cancel = threading.Event()
    permission = FixedPermission(on_check=cancel.set)
    tool = RecordingTool()
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call()]),
        ToolRegistry().register(tool),
    ).run(
        RunRequest(
            make_task(tmp_path),
            permission=permission,
            cancel_event=cancel,
        )
    )

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert permission.calls == 1
    assert tool.calls == 0
    failed = event_payloads(result, EventType.TOOL_EXECUTION_FAILED)[0]
    assert failed["lifecycle_phase"] == "after_permission"


def test_cancel_during_synchronous_tool_keeps_side_effect_and_real_outcome(tmp_path):
    cancel = threading.Event()
    post_calls = []
    tool = RecordingTool(on_execute=cancel.set)
    hooks = Hooks().register(
        HookEvent.POST_TOOL_USE,
        lambda _block, result: post_calls.append(result.success),
    )
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call(), finish()]),
        ToolRegistry().register(tool),
    ).run(RunRequest(make_task(tmp_path), hooks=hooks, cancel_event=cancel))

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert tool.calls == 1
    assert post_calls == [True]
    finished_event = event_payloads(result, EventType.TOOL_EXECUTION_FINISHED)[0]
    assert "cancel_requested_after_tool" in finished_event["diagnostics"]
    assert last_observation(result)["status"] == ObservationStatus.SUCCESS.value


def test_cancel_from_post_hook_keeps_tool_success_then_stops(tmp_path):
    cancel = threading.Event()
    tool = RecordingTool()
    hooks = Hooks().register(HookEvent.POST_TOOL_USE, lambda _block, _result: cancel.set())
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend([call(), finish()]),
        ToolRegistry().register(tool),
    ).run(RunRequest(make_task(tmp_path), hooks=hooks, cancel_event=cancel))

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert tool.calls == 1
    assert len(event_payloads(result, EventType.TOOL_EXECUTION_FINISHED)) == 1
    assert last_observation(result)["status"] == ObservationStatus.SUCCESS.value


# prepare_next_turn / context failure ---------------------------------------


def test_prepare_next_turn_exception_is_infrastructure_and_stops_next_model_call(tmp_path):
    backend = ScriptedFailureBackend([call(), finish()])
    registry = ToolRegistry().register(NoopTool("work"))

    def fail_prepare(_context):
        raise ValueError("deterministic preparation failure")

    result = make_runner(tmp_path, backend, registry).run(
        RunRequest(make_task(tmp_path), prepare_next_turn=fail_prepare)
    )

    assert_terminated(result, RunStatus.FAILED, "infrastructure_error")
    assert backend.call_count == 1
    failed = event_payloads(result, EventType.PREPARE_NEXT_TURN_FAILED)[0]
    assert failed["error_type"] == "ValueError"


def test_prepare_next_turn_cancel_during_callback_is_canceled_before_next_model(tmp_path):
    cancel = threading.Event()
    backend = ScriptedFailureBackend([call(), finish()])
    registry = ToolRegistry().register(NoopTool("work"))

    def cancel_prepare(_context):
        cancel.set()
        return None

    result = make_runner(tmp_path, backend, registry).run(
        RunRequest(
            make_task(tmp_path),
            prepare_next_turn=cancel_prepare,
            cancel_event=cancel,
        )
    )

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert backend.call_count == 1
    assert len(event_payloads(result, EventType.PREPARE_NEXT_TURN_FINISHED)) == 1


def test_shared_history_prepare_failure_uses_same_infrastructure_semantics(tmp_path):
    history = ConversationHistory(max_messages=20)
    history.add(LLMMessage("user", "old request"))
    history.add(LLMMessage("assistant", "old response"))
    backend = ScriptedFailureBackend([finish()])

    def fail_prepare(_context):
        raise RuntimeError("shared boundary failed")

    result = make_runner(tmp_path, backend).run(
        RunRequest(
            make_task(tmp_path),
            history=history,
            prepare_next_turn=fail_prepare,
            entrypoint="chat",
        )
    )

    assert_terminated(result, RunStatus.FAILED, "infrastructure_error")
    assert backend.call_count == 0
    failed = event_payloads(result, EventType.PREPARE_NEXT_TURN_FAILED)[0]
    assert failed["error_type"] == "RuntimeError"


def test_shared_history_prepare_cancel_uses_same_cooperative_boundary(tmp_path):
    history = ConversationHistory(max_messages=20)
    history.add(LLMMessage("user", "old request"))
    history.add(LLMMessage("assistant", "old response"))
    cancel = threading.Event()
    backend = ScriptedFailureBackend([finish()])

    def cancel_prepare(_context):
        cancel.set()
        return None

    result = make_runner(tmp_path, backend).run(
        RunRequest(
            make_task(tmp_path),
            history=history,
            prepare_next_turn=cancel_prepare,
            cancel_event=cancel,
            entrypoint="chat",
        )
    )

    assert_terminated(result, RunStatus.CANCELED, "canceled")
    assert backend.call_count == 0
    assert len(event_payloads(result, EventType.PREPARE_NEXT_TURN_FINISHED)) == 1


# Completion / termination / acceptance / delivery -------------------------


def test_completion_guard_rejects_then_recovers(tmp_path):
    backend = ScriptedFailureBackend([finish("premature"), call("test"), finish("verified")])
    task = make_task(tmp_path, max_steps=3)
    task.require_tests = True
    result = make_runner(
        tmp_path,
        backend,
        ToolRegistry().register(NoopTool("test", "passed")),
    ).run(RunRequest(task))

    assert_terminated(result, RunStatus.SUCCESS, "completion_satisfied")
    assert len(event_payloads(result, EventType.COMPLETION_REJECTED)) == 1


@pytest.mark.parametrize(
    ("script", "max_steps", "status", "reason"),
    [
        ([reflect()], 1, RunStatus.INCOMPLETE, "resource_exhausted"),
        ([give_up()], 1, RunStatus.GAVE_UP, "agent_gave_up"),
    ],
)
def test_resource_exhausted_and_give_up_termination(tmp_path, script, max_steps, status, reason):
    result = make_runner(tmp_path, ScriptedFailureBackend(script)).run(
        RunRequest(make_task(tmp_path, max_steps=max_steps))
    )
    assert_terminated(result, status, reason)
    if status is RunStatus.INCOMPLETE:
        assert result.resource_reason == "max_steps"


def test_loop_detected_is_incomplete_not_failed(tmp_path):
    repeated = [call("noop") for _ in range(6)]
    result = make_runner(
        tmp_path,
        ScriptedFailureBackend(repeated),
        ToolRegistry().register(NoopTool("noop")),
        config=AgentConfig(
            loop_detection_window=2,
            loop_detection_max_period=1,
            reflection_no_edit_steps=100,
        ),
    ).run(RunRequest(make_task(tmp_path, max_steps=6)))

    assert_terminated(result, RunStatus.INCOMPLETE, "loop_detected")
    assert len(event_payloads(result, EventType.LOOP_DETECTED)) == 2


@pytest.mark.parametrize("terminal_case", ["failed", "canceled", "incomplete", "gave_up"])
def test_non_success_agent_status_skips_independent_acceptance(tmp_path, terminal_case):
    verifier_calls = []
    cancel = None
    config = AgentConfig(llm_max_retries=1, llm_retry_delay=0)

    if terminal_case == "failed":
        backend = ScriptedFailureBackend([RuntimeError("provider failed")])
        task = make_task(tmp_path, task_id="accept-failed")
    elif terminal_case == "canceled":
        backend = ScriptedFailureBackend([finish()])
        task = make_task(tmp_path, task_id="accept-canceled")
        cancel = threading.Event()
        cancel.set()
    elif terminal_case == "incomplete":
        backend = ScriptedFailureBackend([reflect()])
        task = make_task(tmp_path, max_steps=1, task_id="accept-incomplete")
    else:
        backend = ScriptedFailureBackend([give_up()])
        task = make_task(tmp_path, task_id="accept-gave-up")

    result = make_runner(tmp_path, backend, config=config).run(
        RunRequest(
            task,
            cancel_event=cancel,
            acceptance=AcceptanceContract(
                verifier=lambda _workspace: verifier_calls.append(True) or True
            ),
        )
    )

    assert result.status is not RunStatus.SUCCESS
    assert result.acceptance_status == "skipped"
    assert verifier_calls == []
    acceptance = event_payloads(result, EventType.ACCEPTANCE)[0]
    assert acceptance["status"] == "skipped"


def test_acceptance_failure_keeps_agent_success_and_blocks_delivery_without_git(tmp_path, monkeypatch):
    result = make_runner(tmp_path, ScriptedFailureBackend([finish()])).run(
        RunRequest(
            make_task(tmp_path),
            acceptance=AcceptanceContract(verifier=lambda _workspace: False),
            entrypoint="github_issue",
        )
    )

    assert result.status is RunStatus.SUCCESS
    assert result.termination_reason == "completion_satisfied"
    assert result.acceptance_status == "failed"

    monkeypatch.setattr(
        github_issue,
        "_run_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("delivery must stop before git")
        ),
    )
    pr = github_issue.deliver_pull_request(
        result,
        local_path=str(tmp_path / "unused"),
        branch="agent/failure",
        commit_message="unused",
        repo_name="owner/repo",
        pr_title="unused",
        pr_body="unused",
        pr_creator=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("PR creator must not run")
        ),
    )
    assert pr is None
    assert result.delivery_status == "blocked_acceptance"


# Trace correlation/redaction and entrypoint consistency --------------------


def test_failure_trace_correlation_and_redaction(tmp_path):
    secret = "provider-secret-123456"
    backend = ScriptedFailureBackend([
        RuntimeError(f"401 Authorization: Bearer {secret}")
    ])
    result = make_runner(
        tmp_path,
        backend,
        config=AgentConfig(llm_max_retries=1),
    ).run(RunRequest(make_task(tmp_path), entrypoint="api"))

    raw = Path(result.trace_path).read_text(encoding="utf-8")
    assert secret not in raw
    events = read_events(result)
    failed = next(
        event.payload for event in events if event.event_type is EventType.LLM_CALL_FAILED
    )
    terminated = next(
        event.payload for event in events if event.event_type is EventType.RUN_TERMINATED
    )
    assert failed["run_id"] == terminated["run_id"]
    assert failed["run_span_id"] == terminated["run_span_id"]
    assert failed["model_call_id"] == failed["span_id"]
    assert terminated["termination_reason"] == "provider_error"
    assert secret not in str(failed)


@pytest.mark.parametrize("entrypoint", ["cli", "chat", "api", "github_issue"])
def test_provider_failure_semantics_are_identical_across_entrypoint_labels(tmp_path, entrypoint):
    backend = ScriptedFailureBackend([RuntimeError("provider rejected request")])
    result = make_runner(
        tmp_path,
        backend,
        config=AgentConfig(llm_max_retries=1),
    ).run(
        RunRequest(
            make_task(tmp_path, task_id=f"entry-{entrypoint}"),
            entrypoint=entrypoint,
        )
    )

    assert_terminated(result, RunStatus.FAILED, "provider_error")
    failed = event_payloads(result, EventType.LLM_CALL_FAILED)[0]
    terminated = event_payloads(result, EventType.RUN_TERMINATED)[0]
    assert failed["entrypoint"] == entrypoint
    assert terminated["entrypoint"] == entrypoint
