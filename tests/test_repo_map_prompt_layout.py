from agent.prompt import build_system_prompt
from llm.base import LLMToolSchema


def test_stable_tool_descriptions_precede_dynamic_repo_map():
    prompt = build_system_prompt(
        "/repo",
        [LLMToolSchema(name="file_read", description="read", parameters={})],
        repo_summary="DYNAMIC_REPO_MAP",
    )
    assert prompt.index("## Available tools") < prompt.index("DYNAMIC_REPO_MAP")
    assert "Path: /repo" in prompt
    assert "file_read" in prompt


def test_system_prompt_prefers_targeted_tools_and_avoids_format_noise():
    prompt = build_system_prompt(
        "/repo",
        [
            LLMToolSchema(name="file_edit", description="edit", parameters={}),
            LLMToolSchema(name="shell", description="shell", parameters={}),
        ],
    )
    assert "Prefer dedicated repository tools over shell" in prompt
    assert "Prefer `file_edit` for a localized change" in prompt
    assert "CRLF/LF" in prompt
    assert "post-edit verification passes" in prompt
