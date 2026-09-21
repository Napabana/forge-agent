"""Fixed offline MCP server for P2-4 evaluation and local protocol E2E."""
from __future__ import annotations

import asyncio

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("forge-eval-mcp")


@mcp.resource("fixture://guidance")
def guidance_resource() -> str:
    """Expose one deterministic resource so capability negotiation is observable."""
    return "Forge MCP fixture guidance resource."


@mcp.prompt()
def guidance_prompt(topic: str = "navigation") -> str:
    """Expose one deterministic prompt; Forge P2-4 records capability only."""
    return f"Inspect repository guidance for {topic}."


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def lookup_project_guidance(topic: str) -> dict[str, str]:
    """Return deterministic repository guidance without network access.

    The fixture tests MCP capability use, not whether a model guesses one exact
    enum-like lookup key. Normalize common natural-language topic variants so
    semantically equivalent policy/config/test queries resolve deterministically.
    """
    normalized = " ".join(
        topic.strip().lower().replace("_", " ").replace("-", " ").split()
    )
    guidance = {
        "configuration": "Use the canonical setting in src/app/config.py rather than hardcoding a duplicate value.",
        "tests": "Run the repository tests after the final code change before finishing.",
        "navigation": "Inspect the canonical configuration module before editing callers.",
        "policy": "Set POLICY_MODE = 'strict' in src/app/config.py; that file is the canonical organization policy setting.",
    }

    if "policy" in normalized:
        guidance_key = "policy"
    elif "config" in normalized or "configuration" in normalized:
        guidance_key = "configuration"
    elif "test" in normalized or "verify" in normalized:
        guidance_key = "tests"
    else:
        guidance_key = "navigation"

    return {
        "topic": normalized,
        "guidance": guidance[guidance_key],
        "source": "forge-eval-mcp-fixture",
    }


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def echo_text(value: str) -> str:
    """Echo deterministic text for protocol/output tests."""
    return f"echo:{value}"


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def record_note(note: str) -> dict[str, str]:
    """Mutation-classified fixture tool; it intentionally performs no real I/O."""
    return {"recorded": note}


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def slow_lookup(delay_seconds: float = 0.2) -> str:
    """Sleep only inside the fixture process so timeout mapping is deterministic."""
    await asyncio.sleep(delay_seconds)
    return "slow-ok"


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def fail_lookup(message: str = "fixture error") -> str:
    """Raise a deterministic application-level tool error."""
    raise ValueError(message)


if __name__ == "__main__":
    mcp.run()
