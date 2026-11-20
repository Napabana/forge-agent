import json
from types import SimpleNamespace

from config.schema import MCPConfig, MCPServerConfig
from durable_runtime.reconcilers import inspect_mcp, mcp_postcondition
from mcp_integration.adapter import MCPToolAdapter
from mcp_integration.manager import MCPToolDescriptor


def test_mcp_only_calls_the_configured_read_only_receipt_query(monkeypatch, record_property):
    server = MCPServerConfig(
        id="jobs", command="fixture", trust_read_only_annotations=True,
        recovery_queries={"submit": {"query_tool": "status", "argument": "job_id"}},
    )
    config = MCPConfig(enabled=True, servers=(server,))
    source = MCPToolDescriptor("jobs", "stdio", "submit", "", {"type": "object"}, {}, 1, True)
    tool = MCPToolAdapter(SimpleNamespace(config=config), source)
    expected = mcp_postcondition(tool, {"job_id": "fixture-1"})
    operation = {"name": tool.name, "expected_postcondition_json": json.dumps(expected)}
    calls = []

    class Manager:
        tools = [SimpleNamespace(remote_name="status", annotations={"read_only_hint": True})]

        def __init__(self, actual):
            assert actual.servers == (server,)

        def start(self):
            pass

        def call_tool(self, server_id, tool_name, params, **kwargs):
            calls.append((server_id, tool_name, params))
            return SimpleNamespace(structured_content={"job_id": "fixture-1", "outcome": "applied"})

        def close(self):
            pass

    monkeypatch.setattr("mcp_integration.manager.MCPClientManager", Manager)
    assert inspect_mcp(operation, config)["outcome"] == "applied"
    assert calls == [("jobs", "status", {"job_id": "fixture-1"})]
    Manager.tools[0].annotations["read_only_hint"] = False
    assert inspect_mcp(operation, config)["outcome"] == "unknown"
    assert len(calls) == 1
    record_property("mcp_receipt_query_correct", 1)
