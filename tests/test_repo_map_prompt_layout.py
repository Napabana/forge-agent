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
