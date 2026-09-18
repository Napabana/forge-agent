from __future__ import annotations

import json
import subprocess
import threading

import pytest

from agent.core import AgentConfig, PrepareNextTurnResult
from agent.event_log import EventLog
from agent.runner import ExecutionRunner, RunRequest
from agent.task import Action, ActionType, EventType, ObservationStatus, RunStatus, Task, ToolCall
from harness import HookEvent, Hooks, PermissionManager
from harness.executor import (
    ToolExecutionCanceled,
    ToolExecutor as RawToolExecutor,
    ToolExecutorInfrastructureError,
)
from harness.permission import ALLOW
from llm.base import MockBackend
from tools.base import BaseTool, ToolErrorType, ToolRegistry, ToolResult


class RecordingTool(BaseTool):
    def __init__(self, name="echo", *, result=None, on_execute=None, calls=None):
        self._name = name
        self._result = result or ToolResult(True, "ok")
        self._on_execute = on_execute
        self.calls = calls if calls is not None else []

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "record test tool"

    @property
    def parameters_schema(self):
        properties = {"text": {"type": "string"}}
        required = []
        if self._name == "shell":
            properties = {"cmd": {"type": "string"}}
            required = ["cmd"]
        return {"type": "object", "properties": properties, "required": required}

    def execute(self, params):
        self.calls.append("tool")
        if self._on_execute is not None:
            self._on_execute()
        return self._result


class RecordingPermission(PermissionManager):
    def __init__(self, calls, *, on_check=None):
        super().__init__()
        self.calls = calls
        self.on_check = on_check

    def check(self, block):
        self.calls.append("permission")
        if self.on_check is not None:
            self.on_check()
        return ALLOW


class BrokenPermission(PermissionManager):
    def check(self, block):
        raise RuntimeError("permission subsystem secret=permission-crash-secret")


def _read_events(path):
    with EventLog.open_existing(path) as log:
        return log.replay()


def _events(events, event_type):
    return [event for event in events if event.event_type is event_type]


def _runner(tmp_path, backend, registry, *, config=None, confirm_callback=None, registry_builder=None):
    return ExecutionRunner(
        backend=backend,
        registry=registry,
        config=config or AgentConfig(),
        log_dir=str(tmp_path / "logs"),
        confirm_callback=confirm_callback,
        registry_builder=registry_builder,
    )


def test_executor_normal_order_is_pre_permission_tool_post():
    calls = []
    hooks = Hooks()
    hooks.register(HookEvent.PRE_TOOL_USE, lambda _block: calls.append("pre"))
    hooks.register(HookEvent.POST_TOOL_USE, lambda _block, _result: calls.append("post"))
    registry = ToolRegistry().register(RecordingTool(calls=calls))

    result = RawToolExecutor(
        registry,
        hooks=hooks,
        permission=RecordingPermission(calls),
    ).execute("echo", {})

    assert result.success
    assert calls == ["pre", "permission", "tool", "post"]


def test_pre_hook_block_and_failure_stop_before_permission_and_tool():
    for hook, expected in (
        (lambda _block: "blocked", ToolErrorType.HOOK_BLOCKED),
        (lambda _block: (_ for _ in ()).throw(RuntimeError("hook boom")), ToolErrorType.HOOK_FAILED),
    ):
        calls = []
        hooks = Hooks().register(HookEvent.PRE_TOOL_USE, hook)
        registry = ToolRegistry().register(RecordingTool(calls=calls))
        result = RawToolExecutor(
            registry,
            hooks=hooks,
            permission=RecordingPermission(calls),
        ).execute("echo", {})
        assert result.error_type is expected
        assert calls == []


def test_validation_stops_before_hook_permission_and_tool():
    calls = []
    hooks = Hooks().register(HookEvent.PRE_TOOL_USE, lambda _block: calls.append("pre"))
    registry = ToolRegistry().register(RecordingTool(calls=calls))
    executor = RawToolExecutor(registry, hooks=hooks, permission=RecordingPermission(calls))

    invalid = executor.execute("echo", {"text": 123})
    unknown = executor.execute("missing", {})

    assert invalid.error_type is ToolErrorType.INVALID_ARGUMENTS
    assert unknown.error_type is ToolErrorType.UNKNOWN_TOOL
    assert calls == []


def test_permission_and_confirm_exceptions_are_infrastructure_failures():
    registry = ToolRegistry().register(RecordingTool("shell"))
    with pytest.raises(ToolExecutorInfrastructureError) as permission_exc:
        RawToolExecutor(registry, permission=BrokenPermission()).execute(
            "shell", {"cmd": "echo safe"}
        )
    assert permission_exc.value.phase == "permission"

    def explode(_cmd):
        raise RuntimeError("confirm crashed")

    with pytest.raises(ToolExecutorInfrastructureError) as confirm_exc:
        RawToolExecutor(
            registry,
            permission=PermissionManager(),
            confirm_callback=explode,
        ).execute("shell", {"cmd": "git commit -m x"})
    assert confirm_exc.value.phase == "permission_confirm"


def test_permission_deny_is_normal_typed_result():
    registry = ToolRegistry().register(RecordingTool("shell"))
    result = RawToolExecutor(registry, permission=PermissionManager()).execute(
        "shell", {"cmd": "rm -rf /"}
    )
    assert not result.success
    assert result.error_type is ToolErrorType.PERMISSION_DENIED


class RecordingRuntime:
    """生产 registry 回归专用：只记录命令，不触发真实 subprocess。"""
    name = "recording"

    def __init__(self):
        self.calls = []

    def exec(self, cmd, cwd=None, timeout=30):
        from tools.runtime import RunResult as RuntimeRunResult
        self.calls.append((cmd, cwd, timeout))
        return RuntimeRunResult(0, "ok\n", "")

    def cleanup(self):
        return None


def _run_production_shell_case(tmp_path, confirm_callback, command="echo changed > value.txt"):
    from config.schema import AppConfig
    from entry.cli import _build_registry

    runtime = RecordingRuntime()
    registry = _build_registry(
        AppConfig(), confirm_callback=confirm_callback, runtime=runtime,
        default_cwd=str(tmp_path), workspace=str(tmp_path),
    )
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run shell", ToolCall("shell", {"cmd": command})),
        Action(ActionType.FINISH, "done", message="done"),
    ])
    result = _runner(
        tmp_path, backend, registry, confirm_callback=confirm_callback,
    ).run(RunRequest(Task("production shell", str(tmp_path), max_steps=2)))
    return result, runtime


def test_production_dangerous_shell_confirms_once_and_executes_once(tmp_path):
    confirmations = []

    def confirm(command):
        confirmations.append(command)
        return True

    result, runtime = _run_production_shell_case(tmp_path, confirm)
    events = _read_events(result.trace_path)
    assert result.status is RunStatus.SUCCESS
    assert confirmations == ["echo changed > value.txt"]
    assert len(runtime.calls) == 1
    decisions = _events(events, EventType.PERMISSION_DECISION)
    assert len(decisions) == 1
    assert decisions[0].payload["decision"] == "confirm"


def test_production_dangerous_shell_rejects_once_without_execution(tmp_path):
    confirmations = []

    def reject(command):
        confirmations.append(command)
        return False

    result, runtime = _run_production_shell_case(tmp_path, reject)
    events = _read_events(result.trace_path)
    assert result.status is RunStatus.SUCCESS
    assert confirmations == ["echo changed > value.txt"]
    assert runtime.calls == []
    observation = _events(events, EventType.OBSERVATION)[0].payload["observation"]
    assert observation["error_type"] == "permission_denied"


def test_production_confirm_callback_crash_is_infrastructure_failure(tmp_path):
    from config.schema import AppConfig
    from entry.cli import _build_registry

    runtime = RecordingRuntime()

    def explode(_command):
        raise RuntimeError("confirm crashed")

    registry = _build_registry(
        AppConfig(), confirm_callback=explode, runtime=runtime,
        default_cwd=str(tmp_path), workspace=str(tmp_path),
    )
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run shell", ToolCall("shell", {"cmd": "echo changed > value.txt"})),
    ])
    result = _runner(
        tmp_path, backend, registry, confirm_callback=explode,
    ).run(RunRequest(Task("confirm crash", str(tmp_path), max_steps=1)))
    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "infrastructure_error"
    assert runtime.calls == []
    events = _read_events(result.trace_path)
    failed = _events(events, EventType.TOOL_EXECUTION_FAILED)[0].payload
    assert failed["lifecycle_phase"] == "permission_confirm"


def test_timeout_observation_uses_timeout_status():
    result = ToolResult(
        False,
        "",
        "command timed out",
        error_type=ToolErrorType.TIMEOUT,
    )
    observation = result.to_observation("shell")
    assert observation.status is ObservationStatus.TIMEOUT
    assert observation.error_type == "timeout"


def test_post_hook_failure_never_overwrites_tool_result():
    hooks = Hooks().register(
        HookEvent.POST_TOOL_USE,
        lambda _block, _result: (_ for _ in ()).throw(RuntimeError("post boom")),
    )
    registry = ToolRegistry().register(RecordingTool(result=ToolResult(True, "real-result")))
    result = RawToolExecutor(registry, hooks=hooks).execute("echo", {})
    assert result.success and result.output == "real-result"
    assert result.diagnostics == ("post_tool_hook:RuntimeError",)


def test_cancel_after_pre_hook_stops_before_permission_and_tool():
    cancel = threading.Event()
    calls = []
    hooks = Hooks().register(
        HookEvent.PRE_TOOL_USE,
        lambda _block: (calls.append("pre"), cancel.set(), None)[-1],
    )
    registry = ToolRegistry().register(RecordingTool(calls=calls))
    executor = RawToolExecutor(
        registry,
        hooks=hooks,
        permission=RecordingPermission(calls),
    )

    with pytest.raises(ToolExecutionCanceled) as exc:
        executor.execute("echo", {}, cancel_event=cancel)
    assert exc.value.phase == "after_pre_hook"
    assert calls == ["pre"]


def test_cancel_after_permission_stops_before_tool():
    cancel = threading.Event()
    calls = []
    registry = ToolRegistry().register(RecordingTool(calls=calls))
    executor = RawToolExecutor(
        registry,
        permission=RecordingPermission(calls, on_check=cancel.set),
    )

    with pytest.raises(ToolExecutionCanceled) as exc:
        executor.execute("echo", {}, cancel_event=cancel)
    assert exc.value.phase == "after_permission"
    assert calls == ["permission"]


def test_cancel_during_tool_preserves_result_and_runs_post_hook():
    cancel = threading.Event()
    calls = []
    hooks = Hooks().register(
        HookEvent.POST_TOOL_USE,
        lambda _block, _result: calls.append("post"),
    )
    registry = ToolRegistry().register(
        RecordingTool(on_execute=cancel.set, calls=calls)
    )
    result = RawToolExecutor(registry, hooks=hooks).execute(
        "echo", {}, cancel_event=cancel
    )

    assert result.success
    assert calls == ["tool", "post"]
    assert "cancel_requested_after_tool" in result.diagnostics


def test_cancel_during_post_hook_preserves_result():
    cancel = threading.Event()
    calls = []

    def post(_block, _result):
        calls.append("post")
        cancel.set()

    hooks = Hooks().register(HookEvent.POST_TOOL_USE, post)
    registry = ToolRegistry().register(RecordingTool(calls=calls))
    result = RawToolExecutor(registry, hooks=hooks).execute(
        "echo", {}, cancel_event=cancel
    )

    assert result.success
    assert calls == ["tool", "post"]
    assert "cancel_requested_after_tool" in result.diagnostics


@pytest.mark.parametrize("entrypoint", ["cli", "chat", "api", "github_issue"])
def test_runner_entrypoints_share_permission_lifecycle_and_trace(tmp_path, entrypoint):
    tool = RecordingTool("shell")
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("shell", {"cmd": "rm -rf /"})),
        Action(ActionType.FINISH, "recover", message="done"),
    ])
    runner = _runner(tmp_path, backend, ToolRegistry().register(tool))
    task = Task(f"{entrypoint} permission", str(tmp_path), max_steps=2)

    result = runner.run(RunRequest(task, entrypoint=entrypoint))
    events = _read_events(result.trace_path)

    assert result.status is RunStatus.SUCCESS
    assert tool.calls == []
    permission = _events(events, EventType.PERMISSION_DECISION)[0].payload
    failed = _events(events, EventType.TOOL_EXECUTION_FAILED)[0].payload
    termination = _events(events, EventType.RUN_TERMINATED)[0].payload
    assert permission["decision"] == "deny"
    assert permission["entrypoint"] == entrypoint
    assert failed["error_type"] == "permission_denied"
    assert termination["termination_reason"] == "completion_satisfied"


def test_pre_hook_failure_is_recoverable_observation(tmp_path):
    hooks = Hooks().register(
        HookEvent.PRE_TOOL_USE,
        lambda _block: (_ for _ in ()).throw(RuntimeError("hook failed")),
    )
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("echo", {})),
        Action(ActionType.FINISH, "recover", message="done"),
    ])
    result = _runner(
        tmp_path,
        backend,
        ToolRegistry().register(RecordingTool()),
    ).run(RunRequest(Task("hook", str(tmp_path), max_steps=2), hooks=hooks))
    events = _read_events(result.trace_path)

    assert result.status is RunStatus.SUCCESS
    failed = _events(events, EventType.TOOL_EXECUTION_FAILED)[0].payload
    observation = _events(events, EventType.OBSERVATION)[0].payload["observation"]
    assert failed["error_type"] == "hook_failed"
    assert observation["error_type"] == "hook_failed"


def test_normal_tool_failure_is_recoverable(tmp_path):
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("echo", {})),
        Action(ActionType.FINISH, "recover", message="done"),
    ])
    registry = ToolRegistry().register(
        RecordingTool(result=ToolResult(False, "", "ordinary failure"))
    )
    result = _runner(tmp_path, backend, registry).run(
        RunRequest(Task("recover", str(tmp_path), max_steps=2))
    )
    assert result.status is RunStatus.SUCCESS
    events = _read_events(result.trace_path)
    assert _events(events, EventType.TOOL_EXECUTION_FAILED)[0].payload["error_type"] == "tool_execution"


def test_permission_subsystem_exception_fails_run_and_is_redacted(tmp_path):
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("echo", {})),
    ])
    result = _runner(
        tmp_path,
        backend,
        ToolRegistry().register(RecordingTool()),
    ).run(RunRequest(
        Task("permission crash", str(tmp_path), max_steps=1),
        permission=BrokenPermission(),
    ))

    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "infrastructure_error"
    raw = open(result.trace_path, encoding="utf-8").read()
    assert "permission-crash-secret" not in raw
    events = _read_events(result.trace_path)
    failed = _events(events, EventType.TOOL_EXECUTION_FAILED)[0].payload
    termination = _events(events, EventType.RUN_TERMINATED)[0].payload
    assert failed["error_type"] == "infrastructure"
    assert failed["lifecycle_phase"] == "permission"
    assert termination["termination_reason"] == "infrastructure_error"


def test_runtime_infrastructure_failure_remains_fatal_guarded(tmp_path):
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("shell", {"cmd": "echo safe"})),
        Action(ActionType.FINISH, "done", message="done"),
    ])
    registry = ToolRegistry().register(RecordingTool(
        "shell",
        result=ToolResult(False, "", "Docker is not available"),
    ))
    result = _runner(tmp_path, backend, registry).run(
        RunRequest(Task("infra", str(tmp_path), max_steps=2))
    )
    assert result.status is RunStatus.FAILED
    assert result.termination_reason == "infrastructure_error"


def test_cancel_immediately_after_tool_records_real_result_then_cancels(tmp_path):
    cancel = threading.Event()
    calls = []
    hooks = Hooks().register(
        HookEvent.POST_TOOL_USE,
        lambda _block, _result: calls.append("post"),
    )
    tool = RecordingTool(on_execute=cancel.set, calls=calls)
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("echo", {})),
    ])
    result = _runner(
        tmp_path,
        backend,
        ToolRegistry().register(tool),
    ).run(RunRequest(
        Task("cancel after tool", str(tmp_path), max_steps=2),
        hooks=hooks,
        cancel_event=cancel,
        entrypoint="api",
    ))
    events = _read_events(result.trace_path)

    assert result.status is RunStatus.CANCELED
    assert result.termination_reason == "canceled"
    assert calls == ["tool", "post"]
    finished = _events(events, EventType.TOOL_EXECUTION_FINISHED)[0].payload
    observation = _events(events, EventType.OBSERVATION)[0].payload["observation"]
    termination = _events(events, EventType.RUN_TERMINATED)[0].payload
    assert "cancel_requested_after_tool" in finished["diagnostics"]
    assert observation["status"] == "success"
    assert termination["status"] == "canceled"
    assert termination["termination_reason"] == "canceled"


def test_cancel_during_post_hook_records_tool_success_then_cancels(tmp_path):
    cancel = threading.Event()

    def post(_block, _result):
        cancel.set()

    hooks = Hooks().register(HookEvent.POST_TOOL_USE, post)
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("echo", {})),
    ])
    result = _runner(
        tmp_path,
        backend,
        ToolRegistry().register(RecordingTool()),
    ).run(RunRequest(
        Task("cancel post", str(tmp_path), max_steps=2),
        hooks=hooks,
        cancel_event=cancel,
        entrypoint="api",
    ))
    events = _read_events(result.trace_path)
    assert result.status is RunStatus.CANCELED
    assert _events(events, EventType.TOOL_EXECUTION_FINISHED)
    assert _events(events, EventType.RUN_TERMINATED)[0].payload["termination_reason"] == "canceled"


def test_cancel_after_model_return_stops_before_action(tmp_path):
    cancel = threading.Event()

    class CancelAfterResponseBackend(MockBackend):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            cancel.set()
            return response

    backend = CancelAfterResponseBackend([
        Action(ActionType.FINISH, "would finish", message="should not win"),
    ])
    result = _runner(tmp_path, backend, ToolRegistry()).run(RunRequest(
        Task("cancel model", str(tmp_path), max_steps=1),
        cancel_event=cancel,
        entrypoint="api",
    ))
    events = _read_events(result.trace_path)

    assert result.status is RunStatus.CANCELED
    assert _events(events, EventType.LLM_CALL_FINISHED)
    assert not _events(events, EventType.ACTION)
    assert _events(events, EventType.RUN_TERMINATED)[0].payload["termination_reason"] == "canceled"


def test_cancel_during_prepare_next_turn_stops_before_next_model_call(tmp_path):
    cancel = threading.Event()
    prepare_calls = []

    def prepare(_context):
        prepare_calls.append("prepare")
        cancel.set()
        return PrepareNextTurnResult()

    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("echo", {})),
    ])
    config = AgentConfig(prepare_next_turn=prepare)
    result = _runner(
        tmp_path,
        backend,
        ToolRegistry().register(RecordingTool()),
        config=config,
    ).run(RunRequest(
        Task("cancel prepare", str(tmp_path), max_steps=3),
        cancel_event=cancel,
        entrypoint="api",
    ))
    events = _read_events(result.trace_path)

    assert result.status is RunStatus.CANCELED
    assert prepare_calls == ["prepare"]
    assert len(_events(events, EventType.LLM_CALL_STARTED)) == 1
    assert _events(events, EventType.PREPARE_NEXT_TURN_FINISHED)
    assert _events(events, EventType.RUN_TERMINATED)[0].payload["termination_reason"] == "canceled"


def _init_git_repo(path):
    path.mkdir()
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_direct_and_isolate_runner_share_permission_denial_semantics(tmp_path):
    direct_backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("shell", {"cmd": "rm -rf /"})),
        Action(ActionType.FINISH, "recover", message="done"),
    ])
    direct = _runner(
        tmp_path / "direct",
        direct_backend,
        ToolRegistry().register(RecordingTool("shell")),
    ).run(RunRequest(Task("direct", str(tmp_path), max_steps=2), entrypoint="cli"))

    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def build_registry(_cfg, _confirm, _runtime, *, default_cwd, workspace):
        return ToolRegistry().register(RecordingTool("shell"))

    isolate_backend = MockBackend([
        Action(ActionType.TOOL_CALL, "try", ToolCall("shell", {"cmd": "rm -rf /"})),
        Action(ActionType.FINISH, "recover", message="done"),
    ])
    isolate_runner = _runner(
        tmp_path / "isolate",
        isolate_backend,
        ToolRegistry(),
        registry_builder=build_registry,
    )
    isolated = isolate_runner.run(RunRequest(
        Task("isolate", str(repo), max_steps=2),
        isolate=True,
        entrypoint="api",
    ))

    assert direct.status is RunStatus.SUCCESS
    assert isolated.status is RunStatus.SUCCESS
    for result in (direct, isolated):
        events = _read_events(result.trace_path)
        observation = _events(events, EventType.OBSERVATION)[0].payload["observation"]
        assert observation["error_type"] == "permission_denied"
        assert _events(events, EventType.PERMISSION_DECISION)[0].payload["decision"] == "deny"
