"""Host-owned candidate feedback and cumulative limits; no provider calls."""
from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from llm.base import MockBackend
from tools.base import BaseTool, NoopTool, ToolRegistry, ToolResult
from tools.test_tool import PytestTool
from agent.completion import CheckOrigin


def tool(name, **params):
    return Action(ActionType.TOOL_CALL, "inspect", ToolCall(name, params))


def run(tmp_path, actions, config=None, *, estimated=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_value.py").write_text("def test_value(): assert True\n")

    class PassingPytest(PytestTool):
        def execute(self, params):
            return ToolResult(params.get("path") != "test_diagnostic.py", "test outcome")

    class Edit(BaseTool):
        name = "edit"
        description = "edit the source"
        parameters_schema = {"type": "object", "properties": {}}

        def execute(self, params):
            (repo / "source.py").write_text("value = 2\n")
            return ToolResult(True, "changed")

    registry = ToolRegistry().register(PassingPytest(default_cwd=str(repo))).register(NoopTool("inspect")).register(Edit())
    task = Task("Inspect the existing tests", str(repo), max_steps=len(actions), require_tests=True)
    backend = MockBackend(actions, estimated=estimated)
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = Agent(backend, registry, config or AgentConfig(repo_map_mode="none")).run(task, log)
    return result, backend, list(log.replay())


def test_pass_requests_wrapup_but_model_must_finish(tmp_path):
    result, backend, events = run(tmp_path, [tool("test"), Action(ActionType.FINISH, "done", message="done")])
    assert result.status is RunStatus.SUCCESS
    assert any(event.event_type is EventType.COMPLETION_CANDIDATE for event in events)
    assert any("Completion candidate" in message.content for message in backend.received_messages[1])


def test_ignored_wrapup_exhausts_without_success(tmp_path):
    result, _, _ = run(tmp_path, [tool("test"), *[tool("inspect") for _ in range(4)]],
                       AgentConfig(repo_map_mode="none", completion_wrapup_steps=2, loop_detection_window=20))
    assert result.status is RunStatus.INCOMPLETE
    assert result.resource_reason == "completion_wrapup"


def test_new_files_are_named_for_review_and_never_deleted(tmp_path):
    result, _, events = run(tmp_path, [tool("edit"), tool("test"), Action(ActionType.FINISH, "done", message="done")])
    candidate = next(event for event in events if event.event_type is EventType.COMPLETION_CANDIDATE)
    assert candidate.payload["state"] == "wrap_up_required"
    assert any("source.py" in reason for reason in candidate.payload["reasons"])
    assert (tmp_path / "repo" / "source.py").exists()
    assert result.status is RunStatus.SUCCESS  # 原 Guard / 独立验收仍有自己的职责。


def test_call_limit_is_normal_incomplete(tmp_path):
    result, backend, _ = run(tmp_path, [tool("inspect"), tool("test")],
                             AgentConfig(repo_map_mode="none", max_model_calls=1))
    assert result.resource_reason == "model_calls"
    assert len(backend.received_messages) == 1


def test_token_limit_is_distinct_from_context_budget(tmp_path):
    result, _, _ = run(tmp_path, [tool("inspect"), tool("test")],
                        AgentConfig(repo_map_mode="none", max_total_tokens=1))
    assert result.resource_reason == "total_tokens"
    assert result.usage_complete is True


def test_new_or_modified_tests_remain_diagnostic(tmp_path):
    path = tmp_path / "test_initial.py"
    path.write_text("def test_initial(): assert True\n")
    baseline = Agent._completion_test_files(str(tmp_path))
    assert Agent._completion_check_origin(str(tmp_path), {"path": "test_initial.py"}, baseline) is CheckOrigin.REPOSITORY
    path.write_text("def test_initial(): assert 1\n")
    assert Agent._completion_check_origin(str(tmp_path), {"path": "test_initial.py"}, baseline) is CheckOrigin.DIAGNOSTIC
    (tmp_path / "test_new.py").write_text("def test_new(): assert True\n")
    assert Agent._completion_check_origin(str(tmp_path), {"path": "test_new.py"}, baseline) is CheckOrigin.DIAGNOSTIC
    assert Agent._completion_check_origin(str(tmp_path), {"path": "../"}, baseline) is CheckOrigin.DIAGNOSTIC


def test_deleted_test_cannot_upgrade_remaining_suite(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a(): assert True\n")
    (tmp_path / "test_b.py").write_text("def test_b(): assert True\n")
    baseline = Agent._completion_test_files(str(tmp_path))
    (tmp_path / "test_b.py").unlink()
    assert Agent._completion_check_origin(str(tmp_path), {"path": "."}, baseline) is CheckOrigin.DIAGNOSTIC


def test_frozen_contract_only_matches_direct_pytest_selection():
    assert Agent._completion_contract_matches("python -m pytest tests/", {})
    assert not Agent._completion_contract_matches("pytest tests/ && echo ok", {})
    assert not Agent._completion_contract_matches("pytest tests/", {"path": "test_new.py"})


def test_edit_after_passing_check_invalidates_candidate(tmp_path):
    result, _, events = run(tmp_path, [tool("test"), tool("edit"), Action(ActionType.FINISH, "done", message="done")])
    assert result.status is RunStatus.INCOMPLETE
    assert any(event.payload.get("code") == "FINAL_STATE_UNVERIFIED" for event in events)


def test_diagnostic_pass_does_not_request_wrapup(tmp_path):
    result, _, events = run(tmp_path, [tool("test", path="test_new.py"), Action(ActionType.GIVE_UP, "cannot prove")])
    assert result.status is RunStatus.GAVE_UP
    assert not any(event.event_type is EventType.COMPLETION_CANDIDATE for event in events)


def test_diagnostic_failure_still_cannot_bypass_guard(tmp_path):
    result, _, events = run(tmp_path, [tool("test"), tool("test", path="test_diagnostic.py"), Action(ActionType.FINISH, "done", message="done")])
    assert result.status is RunStatus.INCOMPLETE
    assert any(event.payload.get("code") == "LATEST_TEST_FAILED" for event in events)


def test_incomplete_usage_fails_closed_with_token_limit(tmp_path):
    result, _, _ = run(tmp_path, [tool("inspect"), tool("test")],
                        AgentConfig(repo_map_mode="none", max_total_tokens=10000), estimated=True)
    assert result.resource_reason == "usage_unavailable"
    assert result.usage_complete is False
