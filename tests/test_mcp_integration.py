from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from agent.core import AgentConfig
from agent.runner import ExecutionRunner, RunRequest
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from config.schema import MCPConfig, MCPServerConfig, _parse
from harness import HookEvent, Hooks, ToolExecutionCanceled, ToolExecutor
from llm.base import MockBackend
from mcp_integration.adapter import MCPToolAdapter, namespaced_tool_name
from mcp_integration.manager import (
    MCPCallTimeout,
    MCPClientManager,
    MCPRemoteFailure,
    MCPToolDescriptor,
)
from mcp_integration.registry import attach_mcp_tools
from tools.base import ToolEffect, ToolErrorType, ToolRegistry


_ROOT = Path(__file__).resolve().parents[1]
_SERVER = _ROOT / "evals" / "fixtures" / "coding_agent" / "mcp" / "server.py"
_CRASH_SERVER = _ROOT / "tests" / "fixtures" / "mcp_crash_server.py"


def _config(*, ids: tuple[str, ...] = ("eval_docs",)) -> MCPConfig:
    return MCPConfig(
        enabled=True,
        servers=tuple(
            MCPServerConfig(
                id=server_id,
                transport="stdio",
                command=sys.executable,
                args=(str(_SERVER),),
                timeout_seconds=5.0,
                trust_read_only_annotations=True,
            )
            for server_id in ids
        ),
    )


def _descriptor(
    *,
    server_id: str = "fixture",
    remote_name: str = "read_value",
    annotations: dict | None = None,
    schema: dict | None = None,
    timeout_seconds: float = 1.0,
    trust_read_only_annotations: bool = True,
) -> MCPToolDescriptor:
    return MCPToolDescriptor(
        server_id=server_id,
        transport="stdio",
        remote_name=remote_name,
        description="fixture tool",
        input_schema=schema
        or {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        annotations=annotations or {},
        timeout_seconds=timeout_seconds,
        trust_read_only_annotations=trust_read_only_annotations,
    )


class _FakeManager:
    def __init__(self, result=None, error: Exception | None = None, event=None):
        self.result = result or SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            structured_content=None,
            is_error=False,
        )
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []
        self.event = event

    def call_tool(self, server_id, remote_name, arguments, *, timeout_seconds):
        self.calls.append((server_id, remote_name, dict(arguments)))
        if self.event is not None:
            self.event.set()
        if self.error is not None:
            raise self.error
        return self.result


def _adapter(manager=None, **kwargs) -> MCPToolAdapter:
    return MCPToolAdapter(manager or _FakeManager(), _descriptor(**kwargs))


def _events(path: str | None) -> list[dict]:
    if path is None:
        return []
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_namespace_is_deterministic_bounded_and_cannot_shadow_native_tools():
    first = namespaced_tool_name("docs-server", "file_read")
    second = namespaced_tool_name("docs-server", "file_read")
    assert first == second == "mcp__docs-server__file_read"
    assert first != "file_read"
    assert len(namespaced_tool_name("server" * 20, "tool" * 40)) <= 64


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"type": "array"},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {}, "required": "value"},
    ],
)
def test_malformed_remote_schema_is_rejected_deterministically(schema):
    with pytest.raises(ValueError, match="inputSchema"):
        _adapter(schema=schema)


def test_remote_annotations_map_conservatively_to_tool_effect():
    read_only = _adapter(annotations={"read_only_hint": True})
    unknown = _adapter(annotations={})
    untrusted_read_only = _adapter(
        annotations={"read_only_hint": True},
        trust_read_only_annotations=False,
    )
    destructive_even_if_not_declared = _adapter(
        annotations={"destructive_hint": True}
    )
    assert read_only.effect is ToolEffect.READ_ONLY
    assert unknown.effect is ToolEffect.MAY_MUTATE_REPOSITORY
    assert untrusted_read_only.effect is ToolEffect.MAY_MUTATE_REPOSITORY
    assert destructive_even_if_not_declared.effect is ToolEffect.MAY_MUTATE_REPOSITORY


def test_text_and_structured_output_are_rendered_without_repr_noise():
    result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="human readable"),
            SimpleNamespace(type="image"),
        ],
        structured_content={"z": 2, "a": 1},
        is_error=False,
    )
    adapter = _adapter(_FakeManager(result))
    tool_result = adapter.execute({"value": "x"})
    assert tool_result.success
    assert "human readable" in tool_result.output
    assert "[image content]" in tool_result.output
    assert '[structured]' in tool_result.output
    assert '"a":1' in tool_result.output


def test_output_is_bounded():
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="x" * 30_000)],
        structured_content=None,
        is_error=False,
    )
    tool_result = _adapter(_FakeManager(result)).execute({"value": "x"})
    assert tool_result.success
    assert len(tool_result.output) <= 16_000
    assert tool_result.output.endswith("[MCP output truncated]")


def test_remote_is_error_is_a_recoverable_tool_observation():
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="remote application error")],
        structured_content=None,
        is_error=True,
    )
    tool_result = _adapter(_FakeManager(result)).execute({"value": "x"})
    assert not tool_result.success
    assert tool_result.error_type is ToolErrorType.TOOL_EXECUTION
    assert "remote application error" in tool_result.error

    invalid_result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Input validation error: bad value")],
        structured_content=None,
        is_error=True,
    )
    invalid = _adapter(_FakeManager(invalid_result)).execute({"value": "x"})
    assert invalid.error_type is ToolErrorType.INVALID_ARGUMENTS


def test_timeout_remote_failure_and_manager_bug_have_distinct_error_types():
    timeout = _adapter(_FakeManager(error=MCPCallTimeout("slow"))).execute({"value": "x"})
    remote = _adapter(_FakeManager(error=MCPRemoteFailure("disconnected"))).execute(
        {"value": "x"}
    )
    invalid = _adapter(
        _FakeManager(error=MCPRemoteFailure("bad args", code=-32602))
    ).execute({"value": "x"})
    assert timeout.error_type is ToolErrorType.TIMEOUT
    assert remote.error_type is ToolErrorType.REMOTE_CAPABILITY
    assert invalid.error_type is ToolErrorType.INVALID_ARGUMENTS


def test_permission_allows_explicit_read_only_and_confirms_unknown_mutation():
    read_manager = _FakeManager()
    read_registry = ToolRegistry().register(
        _adapter(read_manager, annotations={"read_only_hint": True})
    )
    read_result = ToolExecutor(read_registry).execute(
        read_registry.tool_names[0], {"value": "x"}
    )
    assert read_result.success
    assert len(read_manager.calls) == 1

    denied_manager = _FakeManager()
    denied_registry = ToolRegistry().register(_adapter(denied_manager))
    denied = ToolExecutor(denied_registry, confirm_callback=lambda _: False).execute(
        denied_registry.tool_names[0], {"value": "x"}
    )
    assert not denied.success
    assert denied.error_type is ToolErrorType.PERMISSION_DENIED
    assert denied_manager.calls == []

    accepted_manager = _FakeManager()
    accepted_registry = ToolRegistry().register(_adapter(accepted_manager))
    accepted = ToolExecutor(
        accepted_registry, confirm_callback=lambda _: True
    ).execute(accepted_registry.tool_names[0], {"value": "x"})
    assert accepted.success
    assert len(accepted_manager.calls) == 1


def test_hooks_wrap_mcp_tool_like_native_tool():
    manager = _FakeManager()
    adapter = _adapter(manager, annotations={"read_only_hint": True})
    registry = ToolRegistry().register(adapter)
    seen: list[str] = []
    hooks = Hooks()
    hooks.register(HookEvent.PRE_TOOL_USE, lambda block: seen.append("pre"))
    hooks.register(
        HookEvent.POST_TOOL_USE,
        lambda block, result: seen.append("post"),
    )
    result = ToolExecutor(registry, hooks=hooks).execute(
        adapter.name, {"value": "x"}
    )
    assert result.success
    assert seen == ["pre", "post"]
    assert len(manager.calls) == 1


def test_cancel_before_mcp_tool_prevents_remote_call():
    event = threading.Event()
    event.set()
    manager = _FakeManager()
    adapter = _adapter(manager, annotations={"read_only_hint": True})
    registry = ToolRegistry().register(adapter)
    with pytest.raises(ToolExecutionCanceled, match="before_validation"):
        ToolExecutor(registry, cancel_event=event).execute(
            adapter.name, {"value": "x"}
        )
    assert manager.calls == []


def test_cancel_during_sync_bridge_keeps_cooperative_contract():
    event = threading.Event()
    manager = _FakeManager(event=event)
    adapter = _adapter(manager, annotations={"read_only_hint": True})
    registry = ToolRegistry().register(adapter)
    result = ToolExecutor(registry, cancel_event=event).execute(
        adapter.name, {"value": "x"}
    )
    assert result.success
    assert "cancel_requested_after_tool" in result.diagnostics
    assert len(manager.calls) == 1


def test_registry_collision_is_explicit():
    registry = ToolRegistry()
    adapter = _adapter(annotations={"read_only_hint": True})
    registry.register(adapter)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_adapter(annotations={"read_only_hint": True}))


def test_config_defaults_disabled_and_validates_ids_env_and_transport(monkeypatch):
    assert _parse({}).mcp.enabled is False
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "not-logged")
    cfg = _parse(
        {
            "mcp": {
                "enabled": True,
                "servers": [
                    {
                        "id": "docs",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(_SERVER)],
                        "env": {"TOKEN": "explicit"},
                        "timeout_seconds": 2,
                    }
                ],
            }
        }
    )
    assert cfg.mcp.enabled
    assert cfg.mcp.servers[0].env == {"TOKEN": "explicit"}
    with pytest.raises(ValueError, match="duplicate MCP server id"):
        _parse(
            {
                "mcp": {
                    "servers": [
                        {"id": "same", "command": "python"},
                        {"id": "same", "command": "python"},
                    ]
                }
            }
        )
    with pytest.raises(ValueError, match="requires an http"):
        _parse(
            {
                "mcp": {
                    "servers": [
                        {
                            "id": "web",
                            "transport": "streamable_http",
                            "url": "file:///tmp/not-http",
                        }
                    ]
                }
            }
        )


def test_stdio_startup_failure_is_remote_mcp_error_not_forge_infrastructure():
    manager = MCPClientManager(
        MCPConfig(
            enabled=True,
            servers=(
                MCPServerConfig(
                    id="missing",
                    transport="stdio",
                    command="forge-definitely-missing-mcp-command",
                    timeout_seconds=0.5,
                ),
            ),
        )
    )
    with pytest.raises(MCPRemoteFailure, match="startup/discovery failed"):
        manager.start()
    manager.close()


def test_streamable_http_transport_uses_official_client_url_target():
    server = MCPServerConfig(
        id="web",
        transport="streamable_http",
        url="https://example.invalid/mcp",
        timeout_seconds=1.0,
    )
    client = MCPClientManager._make_client(server)
    assert client.server == "https://example.invalid/mcp"


def test_official_sdk_in_process_discover_list_and_call():
    async def scenario() -> None:
        server = MCPServer("in-process-fixture")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            )
        )
        def hello(name: str) -> dict[str, str]:
            return {"hello": name}

        async with Client(server) as client:
            listing = await client.list_tools()
            assert [tool.name for tool in listing.tools] == ["hello"]
            assert listing.tools[0].input_schema["type"] == "object"
            result = await client.call_tool("hello", {"name": "forge"})
            assert not result.is_error
            assert result.structured_content == {"hello": "forge"}

    asyncio.run(scenario())


def test_official_stdio_discover_invoke_structured_error_timeout_and_cleanup():
    manager = MCPClientManager(_config())
    manager.start()
    try:
        descriptors = {descriptor.remote_name: descriptor for descriptor in manager.tools}
        assert {"lookup_project_guidance", "echo_text", "record_note", "slow_lookup", "fail_lookup"} <= set(descriptors)
        assert manager.snapshots[0].tools_supported is True
        assert manager.snapshots[0].resources_supported is True
        assert manager.snapshots[0].prompts_supported is True

        registry = ToolRegistry()
        for descriptor in manager.tools:
            registry.register(MCPToolAdapter(manager, descriptor))

        echo = registry.execute_tool(
            namespaced_tool_name("eval_docs", "echo_text"), {"value": "hello"}
        )
        assert echo.success
        assert "echo:hello" in echo.output

        structured = registry.execute_tool(
            namespaced_tool_name("eval_docs", "lookup_project_guidance"),
            {"topic": "configuration"},
        )
        assert structured.success
        assert "src/app/config.py" in structured.output
        assert "forge-eval-mcp-fixture" in structured.output

        failed = registry.execute_tool(
            namespaced_tool_name("eval_docs", "fail_lookup"),
            {"message": "fixture boom"},
        )
        assert not failed.success
        assert failed.error_type is ToolErrorType.TOOL_EXECUTION

        timeout_adapter = MCPToolAdapter(
            manager,
            MCPToolDescriptor(
                **{
                    **descriptors["slow_lookup"].__dict__,
                    "timeout_seconds": 0.05,
                }
            ),
        )
        timed_out = timeout_adapter.execute({"delay_seconds": 0.3})
        assert not timed_out.success
        assert timed_out.error_type is ToolErrorType.TIMEOUT
    finally:
        manager.close()
    assert not manager.is_started


def test_stdio_server_process_crash_maps_to_remote_capability_and_closes():
    config = MCPConfig(
        enabled=True,
        servers=(
            MCPServerConfig(
                id="crash",
                transport="stdio",
                command=sys.executable,
                args=(str(_CRASH_SERVER),),
                timeout_seconds=2.0,
            ),
        ),
    )
    manager = MCPClientManager(config)
    manager.start()
    try:
        descriptor = next(
            descriptor
            for descriptor in manager.tools
            if descriptor.remote_name == "crash_process"
        )
        result = MCPToolAdapter(manager, descriptor).execute({})
        assert not result.success
        assert result.error_type is ToolErrorType.REMOTE_CAPABILITY
    finally:
        manager.close()
    assert not manager.is_started


def test_multiple_stdio_servers_are_isolated_by_server_id():
    manager = MCPClientManager(_config(ids=("alpha", "beta")))
    manager.start()
    try:
        server_ids = {descriptor.server_id for descriptor in manager.tools}
        assert server_ids == {"alpha", "beta"}
        names = {
            namespaced_tool_name(descriptor.server_id, descriptor.remote_name)
            for descriptor in manager.tools
        }
        assert len(names) == len(manager.tools)
    finally:
        manager.close()


def test_runner_agent_tool_executor_real_stdio_host_e2e(tmp_path: Path):
    backend = MockBackend(
        [
            Action(
                ActionType.TOOL_CALL,
                "use external read-only capability",
                ToolCall(
                    namespaced_tool_name("eval_docs", "echo_text"),
                    {"value": "runner"},
                ),
            ),
            Action(ActionType.FINISH, "done", message="done"),
        ]
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=ToolRegistry(),
        config=AgentConfig(max_steps=4, repo_map_mode="none"),
        log_dir=str(tmp_path / "logs"),
        mcp_config=_config(),
    )
    try:
        result = runner.run(
            RunRequest(
                task=Task(
                    "Use the configured read-only external capability, then finish.",
                    str(tmp_path),
                    task_id="mcp-e2e",
                )
            )
        )
    finally:
        runner.close()

    assert result.status is RunStatus.SUCCESS
    rows = _events(result.trace_path)
    assert any(row["event_type"] == EventType.MCP_TOOL_DISCOVERED.value for row in rows)
    started = [
        row
        for row in rows
        if row["event_type"] == EventType.TOOL_EXECUTION_STARTED.value
        and row["payload"].get("capability_provider") == "mcp"
    ]
    assert len(started) == 1
    assert started[0]["payload"]["mcp_server_id"] == "eval_docs"
    assert started[0]["payload"]["mcp_remote_tool_name"] == "echo_text"
    assert "env" not in started[0]["payload"]


def test_planning_mutation_classification_applies_to_mcp_adapter(tmp_path: Path):
    read_only = _adapter(annotations={"read_only_hint": True})
    mutation_manager = _FakeManager()
    mutation = _adapter(mutation_manager, annotations={})
    registry = ToolRegistry().register(read_only).register(mutation)
    assert registry.is_mutating(read_only.name) is False
    assert registry.is_mutating(mutation.name) is True

    backend = MockBackend(
        [
            Action(
                ActionType.TOOL_CALL,
                "mutation too early",
                ToolCall(mutation.name, {"value": "x"}),
            ),
            Action(ActionType.GIVE_UP, "stop after gate", message="stop"),
        ]
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=AgentConfig(
            max_steps=3,
            repo_map_mode="none",
            planning_mode="always",
        ),
        log_dir=str(tmp_path / "planning-logs"),
    )
    try:
        result = runner.run(
            RunRequest(
                Task(
                    "Change external state after planning.",
                    str(tmp_path),
                    task_id="mcp-plan-gate",
                )
            )
        )
    finally:
        runner.close()
    assert result.status is RunStatus.GAVE_UP
    assert mutation_manager.calls == []
    assert any(row["event_type"] == "plan_rejected" for row in _events(result.trace_path))


def test_remote_capability_failure_is_visible_to_structured_recovery(tmp_path: Path):
    manager = _FakeManager(error=MCPRemoteFailure("server disconnected"))
    adapter = _adapter(manager, annotations={"read_only_hint": True})
    registry = ToolRegistry().register(adapter)
    backend = MockBackend(
        [
            Action(
                ActionType.TOOL_CALL,
                "call remote",
                ToolCall(adapter.name, {"value": "x"}),
            ),
            Action(ActionType.GIVE_UP, "stop", message="stop"),
        ]
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=AgentConfig(
            max_steps=4,
            repo_map_mode="none",
            recovery_mode="structured",
        ),
        log_dir=str(tmp_path / "logs"),
    )
    try:
        result = runner.run(
            RunRequest(Task("Inspect external data.", str(tmp_path), task_id="mcp-recovery"))
        )
    finally:
        runner.close()
    rows = _events(result.trace_path)
    classified = [row for row in rows if row["event_type"] == "failure_classified"]
    assert classified
    assert classified[0]["payload"]["error_type"] == ToolErrorType.REMOTE_CAPABILITY.value


def test_mcp_package_is_in_setuptools_discovery():
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp_integration*"' in pyproject
    assert '"mcp>=2.2.0,<3"' in pyproject
