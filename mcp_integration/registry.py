from __future__ import annotations

from config.schema import MCPConfig
from mcp_integration.adapter import MCPToolAdapter
from mcp_integration.manager import MCPClientManager
from tools.base import ToolRegistry


def attach_mcp_tools(
    registry: ToolRegistry,
    config: MCPConfig | None,
) -> MCPClientManager | None:
    """Discover configured MCP tools and register them as normal Forge tools."""
    if config is None or not config.enabled:
        return None
    manager = MCPClientManager(config)
    manager.start()
    try:
        for descriptor in manager.tools:
            registry.register(MCPToolAdapter(manager, descriptor))
    except BaseException:
        manager.close()
        raise
    return manager
