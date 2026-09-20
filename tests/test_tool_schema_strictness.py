from __future__ import annotations

from unittest.mock import patch

from agent.planning import PlanningRuntime, decide_planning
from agent.task import Task
from config.schema import load_config
from llm.anthropic_backend import _to_anthropic_tool
from llm.base import LLMToolSchema
from llm.openai_compat import _to_openai_tool
from llm.openai_responses import _to_responses_tool
from llm.router import create_backend
from llm.tool_schema import (
    normalize_optional_nulls,
    resolve_tool_schema_capabilities,
    strictify_tool_parameters,
)


def _schema() -> LLMToolSchema:
    return LLMToolSchema(
        name="example",
        description="strict schema example",
        parameters={
            "type": "object",
            "properties": {
                "required_value": {"type": "string"},
                "optional_value": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["required_value"],
        },
    )


def test_strict_schema_conversion_is_provider_compatible_and_nullable_for_optional_fields():
    converted = strictify_tool_parameters(_schema().parameters)

    assert converted["additionalProperties"] is False
    assert converted["required"] == ["required_value", "optional_value", "items"]
    assert converted["properties"]["optional_value"]["type"] == ["string", "null"]
    nested = converted["properties"]["items"]["items"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["name", "note"]
    assert nested["properties"]["note"]["type"] == ["string", "null"]


def test_optional_null_placeholders_are_removed_before_runtime_validation():
    params = {
        "required_value": "x",
        "optional_value": None,
        "items": [{"name": "n", "note": None}],
    }
    normalized = normalize_optional_nulls(params, _schema().parameters)

    assert normalized == {
        "required_value": "x",
        "items": [{"name": "n"}],
    }


def test_auto_is_conservative_for_compatible_proxy_and_unverified_native_endpoint():
    proxy = resolve_tool_schema_capabilities(
        provider="openai",
        protocol="chat_completions",
        base_url="https://proxy.example/v1",
        strict_tool_schema="auto",
    )
    native = resolve_tool_schema_capabilities(
        provider="openai",
        protocol="responses",
        base_url=None,
        strict_tool_schema="auto",
    )

    assert proxy.strict_json_schema is False
    assert native.strict_json_schema is False
    assert "auto-unverified" in native.source


def test_explicit_strict_enable_and_disable_control_backend_contract():
    enabled = resolve_tool_schema_capabilities(
        provider="openai",
        protocol="chat_completions",
        base_url="https://verified-proxy.example/v1",
        strict_tool_schema="on",
    )
    disabled = resolve_tool_schema_capabilities(
        provider="openai",
        protocol="responses",
        base_url=None,
        strict_tool_schema="off",
    )

    assert enabled.strict_json_schema is True
    assert enabled.source == "configured-on"
    assert disabled.strict_json_schema is False
    assert disabled.source == "configured-off"


def test_openai_chat_only_sends_strict_field_when_enabled():
    loose = _to_openai_tool(_schema(), strict=False)
    strict = _to_openai_tool(_schema(), strict=True)

    assert "strict" not in loose["function"]
    assert strict["function"]["strict"] is True
    assert strict["function"]["parameters"]["additionalProperties"] is False


def test_responses_and_anthropic_keep_protocol_specific_shapes():
    loose = _to_responses_tool(_schema(), strict=False)
    strict = _to_responses_tool(_schema(), strict=True)
    anthropic = _to_anthropic_tool(_schema())

    assert loose["strict"] is False
    assert strict["strict"] is True
    assert strict["parameters"]["additionalProperties"] is False
    assert "strict" not in anthropic
    assert anthropic["input_schema"] == _schema().parameters


def test_router_does_not_assume_custom_openai_compatible_proxy_supports_strict():
    with patch("llm.openai_compat.OpenAICompatBackend.__init__", return_value=None):
        backend = create_backend(
            "openai",
            "compatible-model",
            api_key="sk-test",
            base_url="https://proxy.example/v1",
        )

    assert backend.tool_schema_capabilities.strict_json_schema is False


def test_router_uses_strict_when_explicitly_enabled_for_verified_backend():
    with patch("llm.openai_compat.OpenAICompatBackend.__init__", return_value=None):
        backend = create_backend(
            "openai",
            "compatible-model",
            api_key="sk-test",
            base_url="https://verified-proxy.example/v1",
            strict_tool_schema="on",
        )

    assert backend.tool_schema_capabilities.strict_json_schema is True


def test_config_loads_strict_tool_schema_mode(tmp_path):
    path = tmp_path / "strict.yaml"
    path.write_text(
        """
llm:
  provider: openai
  model: compatible-model
  api_key: dummy
  strict_tool_schema: on
agent:
  budget_tokens: 80000
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.llm.strict_tool_schema == "on"


def test_runtime_validation_still_rejects_malformed_planning_payload(tmp_path):
    task = Task("Plan carefully.", str(tmp_path))
    runtime = PlanningRuntime(decide_planning(task, "always"))

    rejected = runtime.apply_control("plan_create", {"goal": "", "steps": []})

    assert rejected.accepted is False
    assert rejected.payload["state_changed"] is False
