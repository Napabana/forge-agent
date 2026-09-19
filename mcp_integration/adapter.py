from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from mcp_integration.manager import (
    MCPCallTimeout,
    MCPClientManager,
    MCPManagerLifecycleError,
    MCPRemoteFailure,
    MCPToolDescriptor,
)
from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolResult

_MAX_TOOL_NAME = 64
_MAX_OUTPUT_CHARS = 16_000
_INVALID_PARAMS = -32602


def _component(value: str, *, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_-") or "tool"
    if cleaned[0].isdigit():
        cleaned = "x_" + cleaned
    if len(cleaned) <= max_len:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:max_len - 9]}_{digest}"


def namespaced_tool_name(server_id: str, remote_name: str) -> str:
    prefix = f"mcp__{_component(server_id, max_len=24)}__"
    remaining = max(8, _MAX_TOOL_NAME - len(prefix))
    return prefix + _component(remote_name, max_len=remaining)


def validate_remote_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(
            "MCP tool inputSchema must be a JSON Schema object with type='object'"
        )
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict):
        raise ValueError("MCP tool inputSchema.properties must be an object")
    if not isinstance(required, list) or any(
        not isinstance(item, str) for item in required
    ):
        raise ValueError("MCP tool inputSchema.required must be a string list")
    try:
        json.dumps(schema, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP tool inputSchema must be JSON serializable") from exc
    return dict(schema)


def _render_call_result(result: Any, *, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    parts: list[str] = []
    for block in tuple(getattr(result, "content", ()) or ()):
        kind = str(getattr(block, "type", "content"))
        if kind == "text" and isinstance(getattr(block, "text", None), str):
            parts.append(block.text)
        else:
            label = f"[{kind} content]"
            uri = getattr(block, "uri", None)
            if uri is not None:
                label = f"[{kind} content: {uri}]"
            parts.append(label)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        parts.append(
            "[structured]\n"
            + json.dumps(
                structured,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    rendered = "\n".join(part for part in parts if part).strip()
    if not rendered:
        rendered = "MCP tool returned no textual or structured content."
    if len(rendered) > max_chars:
        rendered = rendered[: max_chars - 32] + "\n...[MCP output truncated]"
    return rendered


class MCPToolAdapter(BaseTool):
    """Expose one remote MCP tool through the normal synchronous Forge Tool API."""

    def __init__(
        self,
        manager: MCPClientManager,
        descriptor: MCPToolDescriptor,
    ) -> None:
        self._manager = manager
        self._descriptor = descriptor
        self._name = namespaced_tool_name(
            descriptor.server_id,
            descriptor.remote_name,
        )
        self._schema = validate_remote_schema(descriptor.input_schema)
        self._effect = (
            ToolEffect.READ_ONLY
            if (
                descriptor.trust_read_only_annotations
                and descriptor.annotations.get("read_only_hint") is True
            )
            else ToolEffect.MAY_MUTATE_REPOSITORY
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def remote_name(self) -> str:
        return self._descriptor.remote_name

    @property
    def server_id(self) -> str:
        return self._descriptor.server_id

    @property
    def description(self) -> str:
        return self._descriptor.description or (
            f"MCP tool {self._descriptor.remote_name} "
            f"from server {self._descriptor.server_id}."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self._schema

    @property
    def effect(self) -> ToolEffect:
        return self._effect

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "capability_provider": "mcp",
            "mcp_server_id": self._descriptor.server_id,
            "mcp_remote_tool_name": self._descriptor.remote_name,
            "mcp_transport": self._descriptor.transport,
            "mcp_read_only_hint": self._descriptor.annotations.get("read_only_hint"),
            "mcp_destructive_hint": self._descriptor.annotations.get(
                "destructive_hint"
            ),
            "mcp_read_only_annotation_trusted": (
                self._descriptor.trust_read_only_annotations
            ),
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            result = self._manager.call_tool(
                self._descriptor.server_id,
                self._descriptor.remote_name,
                params,
                timeout_seconds=self._descriptor.timeout_seconds,
            )
        except MCPCallTimeout as exc:
            return ToolResult(
                False, "", str(exc), ToolErrorType.TIMEOUT
            )
        except MCPManagerLifecycleError as exc:
            return ToolResult(
                False, "", str(exc), ToolErrorType.INFRASTRUCTURE
            )
        except MCPRemoteFailure as exc:
            error_type = (
                ToolErrorType.INVALID_ARGUMENTS
                if exc.code == _INVALID_PARAMS
                else ToolErrorType.REMOTE_CAPABILITY
            )
            return ToolResult(False, "", str(exc), error_type)

        output = _render_call_result(result)
        if bool(getattr(result, "is_error", False)):
            return ToolResult(
                False,
                output,
                output[:2000],
                ToolErrorType.TOOL_EXECUTION,
            )
        return ToolResult(True, output)
