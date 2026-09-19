"""Test-only MCP server that exits during one tool call to exercise disconnect semantics."""
from __future__ import annotations

import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("forge-mcp-crash-fixture")


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
def crash_process() -> str:
    os._exit(7)


if __name__ == "__main__":
    mcp.run()
