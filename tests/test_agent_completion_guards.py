"""Regression tests for completion verification and infrastructure aborts."""

import subprocess
from types import SimpleNamespace

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import (
    Action, ActionType, EventType, RunStatus, Task, ToolCall, infer_completion_requirements,
)
from llm.base import MockBackend
from tools.base import BaseTool, FailingTool, NoopTool, ToolRegistry, ToolResult


def _tool_action(name: str) -> Action:
    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=f"call {name}",
        tool_call=ToolCall(name=name, params={}),
    )


def _finish_action() -> Action:
    return Action(
        action_type=ActionType.FINISH,
        thought="done",
        message="done",
    )


class RepositoryWriteTool(BaseTool):
    """测试专用：用任意工具名制造真实 repository change。"""

    def __init__(self, name: str, path, content: str = "changed\n") -> None:
        self._name = name
        self._path = path
        self._content = content

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "write a real repository change"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict) -> ToolResult:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(self._content, encoding="utf-8")
        return ToolResult(success=True, output="changed repository")


class RepositoryDeleteTool(RepositoryWriteTool):
    """测试专用：恢复前一步新增文件，使最终 repository state 回到初始状态。"""

    def execute(self, params: dict) -> ToolResult:
        self._path.unlink(missing_ok=True)
        return ToolResult(success=True, output="restored repository")


def _run(tmp_path, script, registry, config=None, *, require_changes=False, require_tests=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    task = Task(
        description="test completion guards",
        repo_path=str(repo),
        max_steps=len(script),
        require_changes=require_changes,
        require_tests=require_tests,
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    backend = MockBackend(script)
    try:
        result = Agent(
            backend,
            registry,
            config=config or AgentConfig(),
        ).run(task, log)
    finally:
        log.close()
    return result, backend, list(log.replay())


def test_finish_rejected_after_failed_test(tmp_path):
    registry = ToolRegistry().register(FailingTool("test", "2 failed"))
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.INCOMPLETE
    assert result.termination_reason == "resource_exhausted"
    assert result.resource_reason == "max_steps"
    assert any(event.event_type == EventType.COMPLETION_REJECTED for event in events)
    assert events[-1].event_type == EventType.TASK_INCOMPLETE


def test_finish_allowed_after_successful_test(tmp_path):
    registry = ToolRegistry().register(NoopTool("test", "2 passed"))
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.SUCCESS
    assert events[-1].event_type == EventType.TASK_COMPLETE


def test_shell_real_change_then_test_satisfies_required_change(tmp_path):
    registry = (
        ToolRegistry()
        .register(RepositoryWriteTool("shell", tmp_path / "repo" / "value.txt"))
        .register(NoopTool("test", "2 passed"))
    )
    result, _, events = _run(
        tmp_path,
        [_tool_action("shell"), _tool_action("test"), _finish_action()],
        registry,
        require_changes=True,
        require_tests=True,
    )
    assert result.status == RunStatus.SUCCESS
    assert not any(event.event_type == EventType.COMPLETION_REJECTED for event in events)


def test_shell_real_change_after_test_requires_retest(tmp_path):
    registry = (
        ToolRegistry()
        .register(NoopTool("test", "2 passed"))
        .register(RepositoryWriteTool("shell", tmp_path / "repo" / "value.txt"))
    )
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _tool_action("shell"), _finish_action()],
        registry,
        require_changes=True,
        require_tests=True,
    )
    assert result.status == RunStatus.INCOMPLETE
    rejection = next(event for event in events if event.event_type == EventType.COMPLETION_REJECTED)
    assert rejection.payload["code"] == "FINAL_STATE_UNVERIFIED"


def test_successful_tool_without_repository_change_does_not_satisfy_required_change(tmp_path):
    result, _, events = _run(
        tmp_path,
        [_tool_action("shell"), _finish_action()],
        ToolRegistry().register(NoopTool("shell", "no change")),
        require_changes=True,
    )
    assert result.status == RunStatus.INCOMPLETE
    rejection = next(event for event in events if event.event_type == EventType.COMPLETION_REJECTED)
    assert rejection.payload["code"] == "REPOSITORY_UNCHANGED"


def test_change_restored_to_initial_state_is_rejected(tmp_path):
    path = tmp_path / "repo" / "temporary.txt"
    registry = (
        ToolRegistry()
        .register(RepositoryWriteTool("shell", path))
        .register(RepositoryDeleteTool("restore", path))
    )
    result, _, events = _run(
        tmp_path,
        [_tool_action("shell"), _tool_action("restore"), _finish_action()],
        registry,
        require_changes=True,
    )
    assert result.status == RunStatus.INCOMPLETE
    rejection = next(event for event in events if event.event_type == EventType.COMPLETION_REJECTED)
    assert rejection.payload["code"] == "REPOSITORY_UNCHANGED"


def test_file_write_real_change_still_satisfies_required_change(tmp_path):
    result, _, _ = _run(
        tmp_path,
        [_tool_action("file_write"), _finish_action()],
        ToolRegistry().register(
            RepositoryWriteTool("file_write", tmp_path / "repo" / "value.txt")
        ),
        require_changes=True,
    )
    assert result.status == RunStatus.SUCCESS


def test_finish_rejected_after_single_fatal_infrastructure_error(tmp_path):
    error = "Failed to start container: Duplicate mount point: /workspace"
    registry = ToolRegistry().register(FailingTool("shell", error))
    result, backend, events = _run(
        tmp_path,
        [_tool_action("shell"), _finish_action()],
        registry,
        AgentConfig(fatal_tool_error_repeats=2),
    )

    assert result.status == RunStatus.FAILED
    assert "unresolved fatal infrastructure error" in (result.error or "").lower()
    assert backend.call_count == 2
    assert events[-1].event_type == EventType.TASK_FAILED


def test_finish_rejected_when_write_follows_successful_test(tmp_path):
    registry = (
        ToolRegistry()
        .register(NoopTool("test", "2 passed"))
        .register(RepositoryWriteTool("file_write", tmp_path / "repo" / "value.txt"))
    )
    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _tool_action("file_write"), _finish_action()],
        registry,
    )

    assert result.status == RunStatus.INCOMPLETE
    rejection = next(event for event in events if event.event_type == EventType.COMPLETION_REJECTED)
    assert rejection.payload["code"] == "FINAL_STATE_UNVERIFIED"


def test_repeated_fatal_infrastructure_error_aborts_early(tmp_path):
    error = "Failed to start container: Duplicate mount point: /workspace"
    registry = ToolRegistry().register(FailingTool("shell", error))
    result, backend, events = _run(
        tmp_path,
        [_tool_action("shell"), _tool_action("shell"), _finish_action()],
        registry,
        AgentConfig(fatal_tool_error_repeats=2),
    )

    assert result.status == RunStatus.FAILED
    assert "duplicate mount point" in (result.error or "")
    assert result.steps_taken == 2
    assert backend.call_count == 2
    assert events[-1].event_type == EventType.TASK_FAILED


def test_finish_rejected_when_required_write_and_test_never_run(tmp_path):
    result, _, events = _run(
        tmp_path,
        [_finish_action()],
        ToolRegistry(),
        require_changes=True,
        require_tests=True,
    )

    assert result.status == RunStatus.INCOMPLETE
    assert events[-1].event_type == EventType.TASK_INCOMPLETE


def test_finish_rejected_when_required_test_never_run(tmp_path):
    registry = ToolRegistry().register(
        RepositoryWriteTool("file_write", tmp_path / "repo" / "value.txt")
    )
    result, _, _ = _run(
        tmp_path,
        [_tool_action("file_write"), _finish_action()],
        registry,
        require_changes=True,
        require_tests=True,
    )

    assert result.status == RunStatus.INCOMPLETE


def test_finish_allowed_when_required_write_was_committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "forge-agent-test@example.invalid"),
        ("git", "config", "user.name", "Forge Agent Test"),
        ("git", "add", "value.txt"),
        ("git", "commit", "-qm", "baseline"),
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)

    class CommitWriteTool(BaseTool):
        @property
        def name(self) -> str:
            return "shell"

        @property
        def description(self) -> str:
            return "write and commit a fixture change"

        @property
        def parameters_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        def execute(self, params: dict) -> ToolResult:
            target.write_text("after\n", encoding="utf-8")
            subprocess.run(("git", "add", "value.txt"), cwd=repo, check=True)
            subprocess.run(
                ("git", "commit", "-qm", "agent change"),
                cwd=repo,
                check=True,
            )
            return ToolResult(success=True, output="written and committed")

    task = Task(
        description="change value.txt",
        repo_path=str(repo),
        max_steps=2,
        require_changes=True,
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    backend = MockBackend([_tool_action("shell"), _finish_action()])
    try:
        result = Agent(
            backend,
            ToolRegistry().register(CommitWriteTool()),
            AgentConfig(),
        ).run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert target.read_text(encoding="utf-8") == "after\n"


def test_git_commit_after_successful_test_does_not_require_retest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "forge-agent-test@example.invalid"),
        ("git", "config", "user.name", "Forge Agent Test"),
        ("git", "add", "value.txt"),
        ("git", "commit", "-qm", "baseline"),
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)

    class CommitExistingChangeTool(BaseTool):
        @property
        def name(self) -> str:
            return "git_commit"

        @property
        def description(self) -> str:
            return "stage and commit the already-tested working tree content"

        @property
        def parameters_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        def execute(self, params: dict) -> ToolResult:
            subprocess.run(("git", "add", "value.txt"), cwd=repo, check=True)
            subprocess.run(
                ("git", "commit", "-qm", "agent change"),
                cwd=repo,
                check=True,
            )
            return ToolResult(success=True, output="committed tested content")

    task = Task(
        description="change and verify value.txt",
        repo_path=str(repo),
        max_steps=4,
        require_changes=True,
        require_tests=True,
    )
    backend = MockBackend([
        _tool_action("file_write"),
        _tool_action("test"),
        _tool_action("git_commit"),
        _finish_action(),
    ])
    registry = (
        ToolRegistry()
        .register(RepositoryWriteTool("file_write", target, "after\n"))
        .register(NoopTool("test", "2 passed"))
        .register(CommitExistingChangeTool())
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    try:
        result = Agent(backend, registry, AgentConfig()).run(task, log)
        events = list(log.replay())
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert target.read_text(encoding="utf-8") == "after\n"
    assert not any(event.event_type == EventType.COMPLETION_REJECTED for event in events)

def test_resource_budget_warning_is_ephemeral_and_hides_exact_steps(tmp_path):
    reflections = [
        Action(action_type=ActionType.REFLECTION, thought=f"reflect {index}")
        for index in range(3)
    ]
    result, backend, _ = _run(
        tmp_path,
        [*reflections, _finish_action()],
        ToolRegistry(),
    )

    assert result.status == RunStatus.SUCCESS
    assert len(backend.received_messages) == 4
    assert not any(
        "[RESOURCE BUDGET LOW]" in message.content
        for message in backend.received_messages[0]
    )

    for messages in backend.received_messages[1:]:
        warnings = [
            message.content
            for message in messages
            if "[RESOURCE BUDGET LOW]" in message.content
        ]
        assert len(warnings) == 1
        assert "model step" not in warnings[0]
        assert not any(token in warnings[0] for token in ("Step ", "3 remaining", "2 remaining", "1 remaining"))


def test_premature_finish_rejection_is_canonical_then_recovers(tmp_path):
    registry = (
        ToolRegistry()
        .register(NoopTool("file_write", "written"))
        .register(NoopTool("test", "2 passed"))
    )
    result, backend, events = _run(
        tmp_path,
        [_tool_action("file_write"), _finish_action(), _tool_action("test"), _finish_action()],
        registry,
        require_tests=True,
    )

    assert result.status == RunStatus.SUCCESS
    assert result.termination_reason == "completion_satisfied"
    rejection = next(event for event in events if event.event_type == EventType.COMPLETION_REJECTED)
    assert rejection.payload["code"] == "REQUIRED_TEST_MISSING"
    assert any(
        "[COMPLETION REJECTED]" in message.content
        for message in backend.received_messages[2]
    )


def test_failed_test_rejection_then_successful_retest_recovers(tmp_path):
    class FlakyTestTool(BaseTool):
        calls = 0

        @property
        def name(self):
            return "test"

        @property
        def description(self):
            return "fails once"

        @property
        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        def execute(self, params):
            self.calls += 1
            return ToolResult(success=self.calls > 1, output="passed" if self.calls > 1 else "failed")

    result, _, events = _run(
        tmp_path,
        [_tool_action("test"), _finish_action(), _tool_action("test"), _finish_action()],
        ToolRegistry().register(FlakyTestTool()),
        require_tests=True,
    )
    assert result.status == RunStatus.SUCCESS
    assert any(
        event.event_type == EventType.COMPLETION_REJECTED
        and event.payload["code"] == "LATEST_TEST_FAILED"
        for event in events
    )


def test_write_after_test_rejection_then_retest_recovers(tmp_path):
    registry = (
        ToolRegistry()
        .register(NoopTool("test", "passed"))
        .register(RepositoryWriteTool("file_write", tmp_path / "repo" / "value.txt"))
    )
    result, _, events = _run(
        tmp_path,
        [
            _tool_action("test"), _tool_action("file_write"), _finish_action(),
            _tool_action("test"), _finish_action(),
        ],
        registry,
    )
    assert result.status == RunStatus.SUCCESS
    assert any(
        event.event_type == EventType.COMPLETION_REJECTED
        and event.payload["code"] == "FINAL_STATE_UNVERIFIED"
        for event in events
    )


def test_infer_completion_requirements_for_coding_task():
    assert infer_completion_requirements(
        "修复 calculator.py，运行 pytest 验证"
    ) == (True, True)
    assert infer_completion_requirements(
        "只读取 README.md，不修改任何文件"
    ) == (False, False)


def test_openai_stop_text_action_is_recovered_as_tool_call():
    from llm.openai_compat import _parse_openai_response

    choice = SimpleNamespace(
        finish_reason="stop",
        message=SimpleNamespace(
            content=(
                'Thought: inspect the bug\n'
                'Action: file_write\n'
                'Params: {"path": "calculator.py", "content": "fixed"}'
            ),
            tool_calls=None,
        ),
    )
    action = _parse_openai_response(choice, choice.message.content)
    assert action.action_type == ActionType.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.name == "file_write"
    assert action.tool_call.params["path"] == "calculator.py"
