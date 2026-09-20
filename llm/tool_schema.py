"""Backend/protocol tool-schema capabilities.

This is intentionally separate from ModelCapabilities: context/output token limits are
model properties, while strict function-schema support is a backend/protocol contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSchemaCapabilities:
    strict_json_schema: bool = False
    source: str = "unknown"


def _mode(value: str | bool | None) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value is None:
        return "auto"
    normalized = str(value).strip().lower()
    if normalized not in {"auto", "on", "off"}:
        raise ValueError("strict_tool_schema must be one of: auto, on, off")
    return normalized


def resolve_tool_schema_capabilities(
    *,
    provider: str,
    protocol: str,
    base_url: str | None,
    strict_tool_schema: str | bool | None = "auto",
) -> ToolSchemaCapabilities:
    mode = _mode(strict_tool_schema)
    if mode == "off":
        return ToolSchemaCapabilities(False, "configured-off")
    if mode == "on":
        return ToolSchemaCapabilities(True, "configured-on")

    provider = provider.strip().lower()
    protocol = protocol.strip().lower().replace("-", "_")
    if provider == "openai" and not base_url and protocol in {
        "chat",
        "chat_completion",
        "chat_completions",
        "responses",
        "response",
    }:
        return ToolSchemaCapabilities(True, "official-openai")
    return ToolSchemaCapabilities(False, "compatible-or-unknown")


def _nullable(schema: dict) -> dict:
    result = deepcopy(schema)
    type_value = result.get("type")
    if isinstance(type_value, str):
        result["type"] = [type_value, "null"]
    elif isinstance(type_value, list) and "null" not in type_value:
        result["type"] = [*type_value, "null"]
    if isinstance(result.get("enum"), list) and None not in result["enum"]:
        result["enum"] = [*result["enum"], None]
    return result


def strictify_tool_parameters(parameters: dict) -> dict:
    """Return an OpenAI-strict-compatible copy without weakening runtime validation."""

    def visit(node: object) -> object:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: visit(value) for key, value in node.items()}
        node_type = result.get("type")
        is_object = node_type == "object" or (
            isinstance(node_type, list) and "object" in node_type
        )
        if is_object:
            properties = result.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            original_required = set(result.get("required") or [])
            strict_properties = {}
            for name, child in properties.items():
                child_schema = child if isinstance(child, dict) else {}
                strict_child = visit(child_schema)
                if name not in original_required and isinstance(strict_child, dict):
                    strict_child = _nullable(strict_child)
                strict_properties[name] = strict_child
            result["properties"] = strict_properties
            result["required"] = list(strict_properties)
            result["additionalProperties"] = False
        return result

    strict = visit(parameters)
    return strict if isinstance(strict, dict) else {}


__all__ = [
    "ToolSchemaCapabilities",
    "resolve_tool_schema_capabilities",
    "strictify_tool_parameters",
]
