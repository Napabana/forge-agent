from agent.core import AgentConfig
from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from context.history import ConversationHistory
from harness import HookEvent, Hooks
from llm.base import LLMMessage, MockBackend
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileWriteTool


def test_runner_owns_direct_lifecycle_and_preserves_history(tmp_path):
    seen = []
    hooks = Hooks().register(HookEvent.PRE_TOOL_USE, lambda block: seen.append(block.name))
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "run", ToolCall("noop", {})),
        Action(ActionType.FINISH, "done", message="Done."),
    ])
    history = ConversationHistory()
    history.add(LLMMessage("user", "existing session context"))
    task = Task("Run it.", str(tmp_path), task_id="runner-direct", max_steps=2)
    runner = ExecutionRunner(
        backend=backend,
        registry=ToolRegistry().register(NoopTool("noop")),
        config=AgentConfig(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(task, history=history, hooks=hooks))

    assert result.status is RunStatus.SUCCESS
    assert result.trace_path and seen == ["noop"]
    assert any(message.content == "existing session context" for message in backend.received_messages[0])


def test_runner_freezes_acceptance_before_agent_run(tmp_path):
    task = Task("Finish without edits.", str(tmp_path), task_id="runner-acceptance")
    runner = ExecutionRunner(
        backend=MockBackend([Action(ActionType.FINISH, "done", message="Done.")]),
        registry=ToolRegistry(),
        log_dir=str(tmp_path / "logs"),
    )

    result = runner.run(RunRequest(
        task,
        acceptance=AcceptanceContract(require_changes=True),
    ))

    assert result.status is RunStatus.FAILED
    assert "requires repository changes" in result.summary


def test_runner_keeps_hidden_verifier_outside_history_and_records_acceptance(tmp_path):
    repo = tmp_path / "accepted"
    repo.mkdir()
    (repo / "forbidden.py").write_text("USER_CHANGE = True\n")  # 运行前已有修改不能算作 Agent 改动。
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "写入允许文件", ToolCall("file_write", {"path": "allowed.py", "content": "VALUE = 1\n"})),
        Action(ActionType.FINISH, "完成", message="Done."),
    ])

    def hidden_verifier(workspace):
        """HIDDEN_VERIFIER_SENTINEL"""
        return (workspace / "allowed.py").read_text() == "VALUE = 1\n"

    runner = ExecutionRunner(backend=backend, registry=ToolRegistry().register(FileWriteTool(workspace=repo)), log_dir=str(tmp_path / "logs"))
    task = Task("Create allowed.py.", str(repo), task_id="runner-hidden", max_steps=2)
    contract = AcceptanceContract(require_changes=True, required_paths=("allowed.py",), forbidden_paths=("forbidden.py",), verifier=hidden_verifier)
    result = runner.run(RunRequest(task, acceptance=contract))

    assert result.status is RunStatus.SUCCESS
    assert result.acceptance_status == "passed" and result.acceptance_error is None
    assert result.delivery_status == "not_requested"
    assert all("HIDDEN_VERIFIER_SENTINEL" not in message.content for turn in backend.received_messages for message in turn)


def test_runner_preserves_agent_success_when_forbidden_path_fails_acceptance(tmp_path):
    repo = tmp_path / "rejected"
    repo.mkdir()
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "写入禁改文件", ToolCall("file_write", {"path": "forbidden.py", "content": "VALUE = 1\n"})),
        Action(ActionType.FINISH, "完成", message="Done."),
    ])
    runner = ExecutionRunner(backend=backend, registry=ToolRegistry().register(FileWriteTool(workspace=repo)), log_dir=str(tmp_path / "logs"))
    task = Task("Create a file.", str(repo), task_id="runner-rejected", max_steps=2)
    result = runner.run(RunRequest(task, acceptance=AcceptanceContract(require_changes=True, forbidden_paths=("forbidden.py",))))

    assert result.status is RunStatus.SUCCESS
    assert result.acceptance_status == "failed"
    assert result.acceptance_error == "forbidden paths changed: forbidden.py"
    assert result.delivery_status == "not_requested"
