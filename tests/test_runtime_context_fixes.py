from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.core import Agent, AgentConfig
from agent.runner import _merge_context_policy_usage
from agent.task import RunResult as AgentRunResult, RunStatus
from config.schema import _parse
from context.token_budget import TokenBudget
from entry.context_common import build_context_policy
from harness.executor import ToolExecutor
from harness.permission import Decision, PermissionDecision
from llm.base import LLMMessage, MockBackend
from llm.usage import SessionUsage
from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolRegistry, ToolResult
from tools.git_tool import _run_git
from tools.runtime import DockerRuntime, LocalRuntime, RunResult
from tools.test_tool import PytestTool


def _minimal_config(context: dict | None = None, tools: dict | None = None):
    return _parse(
        {
            "agent": {
                "context_budget_cap": 100_000,
                "context_safety_margin_tokens": 1_000,
            },
            "context": context or {},
            "tools": tools or {},
        }
    )


def test_context_config_parses_repo_map_and_compaction_policies():
    config = _minimal_config(
        {
            "repo_map_mode": "query_aware",
            "repo_map_budget": 6_000,
            "compaction_mode": "traceable",
            "compaction_threshold": 0.72,
            "compaction_target_ratio": 0.48,
            "compaction_keep_recent_tokens": 4_000,
            "history_window": 12,
        },
        {"test": {"python_executable": "/opt/venv/bin/python"}},
    )

    assert config.context.repo_map_mode == "query_aware"
    assert config.context.repo_map_budget == 6_000
    assert config.context.compaction_mode == "traceable"
    assert config.context.compaction_threshold == 0.72
    assert config.context.compaction_target_ratio == 0.48
    assert config.context.compaction_keep_recent_tokens == 4_000
    assert config.context.history_window == 12
    assert config.tools.test.python_executable == "/opt/venv/bin/python"


@pytest.mark.parametrize(
    "mode",
    ["none", "static", "query_aware", "incremental"],
)
def test_repo_map_mode_accepts_all_supported_modes(mode):
    assert _minimal_config({"repo_map_mode": mode}).context.repo_map_mode == mode


@pytest.mark.parametrize("mode", ["off", "persistent", 0, True])
def test_repo_map_mode_rejects_unsupported_modes(mode):
    with pytest.raises(ValueError, match="context.repo_map_mode"):
        _minimal_config({"repo_map_mode": mode})


def test_context_config_accepts_explicit_compaction_off_and_rejects_bad_ratios():
    config = _minimal_config({"compaction_mode": False})
    assert config.context.compaction_mode == "off"

    with pytest.raises(ValueError, match="compaction ratios"):
        _minimal_config(
            {
                "compaction_threshold": 0.5,
                "compaction_target_ratio": 0.6,
            }
        )


def test_context_policy_factory_creates_isolated_configured_policy():
    config = _minimal_config(
        {
            "compaction_mode": "traceable",
            "compaction_threshold": 0.7,
            "compaction_target_ratio": 0.5,
            "compaction_keep_recent_tokens": 2_000,
        }
    )
    policy = build_context_policy(config, MockBackend([]))

    assert policy is not None
    assert policy.threshold == 0.7
    assert policy.target_ratio == 0.5
    assert policy.keep_recent_tokens == 2_000
    assert build_context_policy(
        _minimal_config({"compaction_mode": "off"}), MockBackend([])
    ) is None


def test_repo_map_budget_is_a_hard_cap_over_the_dynamic_share():
    capped = TokenBudget(total=100_000, repo_map_cap=5_000)
    uncapped_by_policy = TokenBudget(total=100_000, repo_map_cap=20_000)

    assert capped.default_plan().repo_map == 5_000
    assert uncapped_by_policy.default_plan().repo_map == 12_750

    with pytest.raises(ValueError, match="repo_map_cap"):
        TokenBudget(total=100_000, repo_map_cap=0)


class _RecordingRuntime:
    python_executable = "/runtime/python"

    def __init__(self):
        self.args = None
        self.cwd = None
        self.timeout = None

    def exec_args(self, args, cwd=None, timeout=30):
        self.args = list(args)
        self.cwd = cwd
        self.timeout = timeout
        return RunResult(returncode=0, stdout="2 passed in 0.01s\n", stderr="")


def test_pytest_tool_uses_runtime_interpreter_and_preserves_argv(tmp_path):
    runtime = _RecordingRuntime()
    target = "tests/test_a.py; touch SHOULD_NOT_RUN"
    tool = PytestTool(runtime=runtime, default_cwd=str(tmp_path))

    result = tool.execute(
        {
            "path": target,
            "args": "-x -k 'smoke test'",
        }
    )

    assert result.success
    assert runtime.args[0] == "/runtime/python"
    assert runtime.args[1:3] == ["-m", "pytest"]
    assert "-x" in runtime.args
    assert "smoke test" in runtime.args
    assert runtime.args[-1] == target
    assert runtime.cwd == str(tmp_path)
    assert runtime.timeout == 120


def test_pytest_tool_allows_explicit_interpreter_override(tmp_path):
    runtime = _RecordingRuntime()
    tool = PytestTool(
        runtime=runtime,
        default_cwd=str(tmp_path),
        python_executable="/custom/python3.12",
    )

    result = tool.execute({"path": "tests"})

    assert result.success
    assert runtime.args[0] == "/custom/python3.12"


def test_pytest_tool_rejects_option_like_path_before_execution(tmp_path):
    runtime = _RecordingRuntime()
    tool = PytestTool(runtime=runtime, default_cwd=str(tmp_path))

    result = tool.execute({"path": "--help"})

    assert not result.success
    assert result.error_type is ToolErrorType.INVALID_ARGUMENTS
    assert runtime.args is None


@pytest.mark.parametrize("returncode", [1, 5])
def test_pytest_tool_classifies_test_outcomes_as_tool_failures(tmp_path, returncode):
    runtime = _RecordingRuntime()
    runtime.exec_args = lambda *args, **kwargs: RunResult(returncode, "1 failed\n", "")

    result = PytestTool(runtime=runtime, default_cwd=str(tmp_path)).execute({"path": "tests"})

    assert not result.success
    assert result.error_type is ToolErrorType.TOOL_EXECUTION


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_error"),
    [
        (1, "/test/python: No module named pytest\n", "pytest is not installed"),
        (3, "INTERNALERROR> plugin crashed\n", "pytest runtime failed"),
        (-1, "No such file or directory\n", "could not be started"),
    ],
)
def test_pytest_tool_classifies_environment_failures_as_infrastructure(
    tmp_path, returncode, stderr, expected_error
):
    runtime = _RecordingRuntime()
    runtime.exec_args = lambda *args, **kwargs: RunResult(returncode, "", stderr)

    result = PytestTool(runtime=runtime, default_cwd=str(tmp_path)).execute({"path": "tests"})

    assert not result.success
    assert result.error_type is ToolErrorType.INFRASTRUCTURE
    assert expected_error in result.error


def test_local_runtime_exec_args_never_invokes_a_shell(monkeypatch, tmp_path):
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("tools.runtime.subprocess.run", fake_run)
    result = LocalRuntime().exec_args(["python", "-c", "print(';')"], cwd=str(tmp_path))

    assert result.success
    assert calls["args"] == ["python", "-c", "print(';')"]
    assert calls["kwargs"]["shell"] is False


def test_docker_runtime_exec_args_uses_direct_docker_argv(monkeypatch, tmp_path):
    runtime = DockerRuntime(repo_path=str(tmp_path))
    runtime._container_id = "container-id"
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("tools.runtime.subprocess.run", fake_run)
    result = runtime.exec_args(["python3", "-m", "pytest", "a;b.py"], cwd=str(tmp_path))

    assert result.success
    assert calls["args"] == [
        "docker", "exec", "--workdir", "/workspace", "container-id",
        "python3", "-m", "pytest", "a;b.py",
    ]
    assert calls["kwargs"]["shell"] is False


def test_git_tool_executes_git_as_argv():
    runtime = _RecordingRuntime()
    success, output = _run_git(
        ["diff", "--", "src/file; touch SHOULD_NOT_RUN.py"],
        cwd="/workspace",
        runtime=runtime,
    )

    assert success
    assert output == "2 passed in 0.01s"
    assert runtime.args == [
        "git", "diff", "--", "src/file; touch SHOULD_NOT_RUN.py",
    ]


class _ConfirmPermission:
    def check(self, block):
        return PermissionDecision(Decision.CONFIRM, "requires approval")


class _SideEffectTool(BaseTool):
    executed = False

    @property
    def name(self):
        return "side_effect"

    @property
    def description(self):
        return "test tool"

    @property
    def effect(self):
        return ToolEffect.MAY_MUTATE_REPOSITORY

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params):
        self.executed = True
        return ToolResult(success=True, output="executed")


def test_confirm_without_interactive_channel_returns_actionable_denial():
    tool = _SideEffectTool()
    executor = ToolExecutor(
        ToolRegistry().register(tool),
        permission=_ConfirmPermission(),
        confirm_callback=None,
    )

    result = executor.execute("side_effect", {})

    assert not result.success
    assert not tool.executed
    assert "no interactive approval channel" in result.error
    assert "The operation was not executed" in result.error


def test_trace_breakdown_uses_the_token_budget_counter():
    class FixedCounter:
        name = "fixed-test-counter"
        estimated = True

        def count_text(self, value):
            return len(value)

        def count_message(self, message):
            return len(str(message.get("role", ""))) + len(str(message.get("content", ""))) + 4

        def count_messages(self, messages):
            return sum(self.count_message(message) for message in messages)

        def count_tool_schemas(self, tools):
            return len(tools) * 7

    counter = FixedCounter()
    budget = TokenBudget(total=10_000, token_counter=counter)
    agent = Agent(MockBackend([]), ToolRegistry(), AgentConfig())
    agent._repo_map_cache = "repo map"
    agent._planning_context_cache = "plan"

    breakdown = agent._trace_token_breakdown(
        [
            LLMMessage(role="system", content="system repo map plan"),
            LLMMessage(role="user", content="task"),
        ],
        [],
        token_budget=budget,
    )

    assert breakdown["counter_name"] == "fixed-test-counter"
    assert breakdown["counter_estimated"] is True
    assert breakdown["repo_map_tokens"] == len("repo map")


def test_runner_merges_semantic_compaction_usage_once():
    class OneShotPolicy:
        def __init__(self):
            self.pending = SessionUsage(
                llm_calls=1,
                input_tokens=20,
                output_tokens=10,
            )

        def consume_usage(self):
            result = self.pending
            self.pending = SessionUsage()
            return result

    policy = OneShotPolicy()
    result = AgentRunResult(
        task_id="task",
        status=RunStatus.SUCCESS,
        summary="done",
        steps_taken=1,
    )

    _merge_context_policy_usage(result, policy)
    _merge_context_policy_usage(result, policy)

    assert result.usage.llm_calls == 1
    assert result.total_tokens == 30


def test_docker_preflight_uses_the_configured_python_executable(monkeypatch, tmp_path):
    runtime = DockerRuntime(
        repo_path=str(tmp_path),
        python_executable="/opt/custom venv/bin/python",
    )
    captured = {}

    def fake_exec(cmd, cwd=None, timeout=30):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return RunResult(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runtime, "exec", fake_exec)
    result = runtime.preflight(cwd=str(tmp_path))

    assert result.success
    assert "'/opt/custom venv/bin/python' -m pytest --version" in captured["cmd"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 30


def test_context_config_defaults_are_aligned_for_production_entries():
    config = _minimal_config()

    assert config.context.repo_map_mode == "incremental"
    assert config.context.repo_map_budget == 12_000
    assert config.context.compaction_mode == "traceable"
    assert config.context.compaction_threshold == 0.70
    assert config.context.compaction_target_ratio == 0.50
    assert config.context.compaction_keep_recent_tokens == 8_000


def test_runtime_python_defaults_are_environment_specific():
    import sys

    assert LocalRuntime().python_executable == sys.executable
    assert DockerRuntime(repo_path=".").python_executable == "python3"


def test_programmatic_agent_config_does_not_change_legacy_repo_map_budget():
    # Product entrypoints explicitly forward ContextConfig.repo_map_budget.
    # Standalone AgentConfig users and existing frozen ablations keep old behavior.
    assert AgentConfig().repo_map_budget is None
    assert _minimal_config().context.repo_map_budget == 12_000


def test_local_runtime_subclass_exec_override_is_respected(monkeypatch, tmp_path):
    seen = []

    class InterceptRuntime(LocalRuntime):
        def exec(self, cmd, cwd=None, timeout=30):
            seen.append((cmd, cwd, timeout))
            return RunResult(0, "ok", "")

    result = InterceptRuntime().exec_args(
        ["git", "status", "--short", "file;name.py"],
        cwd=str(tmp_path),
    )

    assert result.success
    assert len(seen) == 1
    assert seen[0][0].startswith("git status --short ")
    assert "'file;name.py'" in seen[0][0]
    assert seen[0][1] == str(tmp_path)
