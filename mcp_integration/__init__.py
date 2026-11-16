"""MCP client integration: external capabilities adapted into Forge Tools."""

from mcp_integration.adapter import MCPToolAdapter, namespaced_tool_name
from mcp_integration.manager import (
    MCPCallTimeout,
    MCPClientManager,
    MCPIntegrationError,
    MCPManagerLifecycleError,
    MCPRemoteFailure,
    MCPServerSnapshot,
    MCPToolDescriptor,
)
from mcp_integration.registry import attach_mcp_tools

__all__ = [
    "MCPClientManager",
    "MCPToolAdapter",
    "MCPToolDescriptor",
    "MCPServerSnapshot",
    "MCPIntegrationError",
    "MCPManagerLifecycleError",
    "MCPCallTimeout",
    "MCPRemoteFailure",
    "attach_mcp_tools",
    "namespaced_tool_name",
]
