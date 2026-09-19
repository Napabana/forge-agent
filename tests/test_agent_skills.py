from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core import Agent, AgentConfig, PrepareNextTurnResult
from agent.event_log import EventLog
from evals.coding_agent.__main__ import (
    _planning_mode_for_variant,
    _recovery_mode_for_variant,
    _skills_enabled_for_variant,
)
from agent.task import Action, ActionType, EventType, RunStatus, Task, ToolCall
from skills.catalog import SkillCatalog
from llm.base import MockBackend
from tools.base import ToolRegistry


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str,
    body: str,
    reference: tuple[str, str] | None = None,
    script: tuple[str, str] | None = None,
) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    if reference is not None:
        path, content = reference
        target = skill / "references" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if script is not None:
        path, content = script
        target = skill / "scripts" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill


def _run(
    tmp_path: Path,
    script: list[Action],
    *,
    repo: Path,
    global_root: Path | None = None,
    max_loaded: int = 3,
    prepare_next_turn=None,
):
    task = Task("Fix the repository carefully.", str(repo), task_id="skills-test")
    backend = MockBackend(script)
    agent = Agent(
        backend,
        ToolRegistry(),
        AgentConfig(
            max_steps=max(6, len(script) + 2),
            repo_map_mode="none",
            skills_enabled=True,
            skills_global_dir=str(global_root) if global_root is not None else None,
            skills_max_loaded=max_loaded,
            prepare_next_turn=prepare_next_turn,
        ),
    )
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    try:
        result = agent.run(task, log)
        trace = Path(log.path)
    finally:
        log.close()
    rows = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return result, agent, backend, rows


def _load(name: str) -> Action:
    return Action(
        ActionType.TOOL_CALL,
        f"load {name}",
        ToolCall("skill_load", {"name": name}),
    )


def _reference(skill: str, reference: str) -> Action:
    return Action(
        ActionType.TOOL_CALL,
        "load reference",
        ToolCall(
            "skill_reference_load",
            {"skill": skill, "reference": reference},
        ),
    )


def _finish() -> Action:
    return Action(ActionType.FINISH, "done", message="done")


def _events(rows: list[dict], event_type: EventType) -> list[dict]:
    return [row for row in rows if row["event_type"] == event_type.value]


def test_metadata_is_visible_but_full_skill_is_progressively_disclosed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "FULL-SKILL-BODY-ONLY-AFTER-LOAD"
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="Use for debugging failing behavior.",
        body=marker,
    )

    result, agent, backend, rows = _run(
        tmp_path,
        [_load("bug-fix"), _finish()],
        repo=repo,
    )

    assert result.status is RunStatus.SUCCESS
    assert agent.loaded_skills == ("bug-fix",)
    first_system = backend.received_messages[0][0].content
    second_system = backend.received_messages[1][0].content
    assert "bug-fix: Use for debugging failing behavior." in first_system
    assert marker not in first_system
    assert marker in second_system
    assert len(_events(rows, EventType.SKILL_DISCOVERED)) == 1
    assert len(_events(rows, EventType.SKILL_SELECTED)) == 1
    assert len(_events(rows, EventType.SKILL_LOADED)) == 1


def test_should_not_trigger_keeps_skill_body_out_of_context(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "SHOULD-NOT-BE-LOADED"
    _write_skill(
        repo / ".agents" / "skills",
        "test-and-verify",
        description="Use when verification is required.",
        body=marker,
    )

    result, agent, backend, rows = _run(tmp_path, [_finish()], repo=repo)

    assert result.status is RunStatus.SUCCESS
    assert agent.loaded_skills == ()
    assert marker not in backend.received_messages[0][0].content
    assert _events(rows, EventType.SKILL_SELECTED) == []
    assert _events(rows, EventType.SKILL_LOADED) == []


def test_reference_is_loaded_only_after_skill_and_only_on_demand(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    reference_marker = "REFERENCE-CONTENT-ON-DEMAND"
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="Use for debugging.",
        body="Main workflow.",
        reference=("checklist.md", reference_marker),
    )

    result, _, backend, rows = _run(
        tmp_path,
        [_load("bug-fix"), _reference("bug-fix", "checklist.md"), _finish()],
        repo=repo,
    )

    assert result.status is RunStatus.SUCCESS
    assert reference_marker not in backend.received_messages[0][0].content
    assert reference_marker not in backend.received_messages[1][0].content
    assert reference_marker in backend.received_messages[2][0].content
    loaded = _events(rows, EventType.SKILL_REFERENCE_LOADED)
    assert len(loaded) == 1
    assert loaded[0]["payload"]["reference"] == "checklist.md"


def test_reference_requires_loaded_skill_and_rejects_unknown_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="Use for debugging.",
        body="Main workflow.",
        reference=("checklist.md", "safe"),
    )

    result, agent, _, rows = _run(
        tmp_path,
        [
            _reference("bug-fix", "checklist.md"),
            _load("bug-fix"),
            _reference("bug-fix", "../secret.txt"),
            _finish(),
        ],
        repo=repo,
    )

    assert result.status is RunStatus.SUCCESS
    assert agent.loaded_skills == ("bug-fix",)
    rejected = _events(rows, EventType.SKILL_REJECTED)
    assert len(rejected) == 2


def test_loaded_skill_survives_history_override_compaction_like_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "SKILL-SURVIVES-HISTORY-OVERRIDE"
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="Use for debugging.",
        body=marker,
    )

    def compact_like(context):
        return PrepareNextTurnResult(history_override=(context.history.to_list()[0],))

    result, _, backend, _ = _run(
        tmp_path,
        [_load("bug-fix"), _finish()],
        repo=repo,
        prepare_next_turn=compact_like,
    )

    assert result.status is RunStatus.SUCCESS
    assert marker in backend.received_messages[1][0].content


def test_project_skill_overrides_global_and_bad_skill_isolated(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    global_root = tmp_path / "global-skills"
    _write_skill(
        global_root,
        "bug-fix",
        description="global description",
        body="GLOBAL BODY",
    )
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="project description",
        body="PROJECT BODY",
    )

    bad = global_root / "bad-dir"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text(
        "---\nname: different-name\ndescription: invalid\n---\nbody\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover(repo, global_root=global_root)
    selected = catalog.get("bug-fix")
    assert selected is not None
    assert selected.source == "project"
    assert "PROJECT BODY" in selected.instructions
    assert len(catalog.issues) == 1
    assert catalog.issues[0].directory == "bad-dir"


def test_script_manifest_never_executes_script(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / "SCRIPT_EXECUTED"
    _write_skill(
        repo / ".agents" / "skills",
        "automation",
        description="Use for a scripted repository workflow.",
        body="Inspect the script before deciding whether to run it.",
        script=("touch_marker.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n"),
    )

    result, _, backend, rows = _run(
        tmp_path,
        [_load("automation"), _finish()],
        repo=repo,
    )

    assert result.status is RunStatus.SUCCESS
    assert not marker.exists()
    assert "touch_marker.py" in backend.received_messages[1][0].content
    assert _events(rows, EventType.TOOL_EXECUTION_STARTED) == []


def test_loaded_skill_limit_is_bounded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = repo / ".agents" / "skills"
    _write_skill(root, "one", description="first skill", body="ONE")
    _write_skill(root, "two", description="second skill", body="TWO")

    result, agent, _, rows = _run(
        tmp_path,
        [_load("one"), _load("two"), _finish()],
        repo=repo,
        max_loaded=1,
    )

    assert result.status is RunStatus.SUCCESS
    assert agent.loaded_skills == ("one",)
    rejected = _events(rows, EventType.SKILL_REJECTED)
    assert len(rejected) == 1
    assert "loaded skill limit reached" in rejected[0]["payload"]["error"]


def test_catalog_rejects_reference_traversal_even_after_discovery(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_skill(
        repo / ".agents" / "skills",
        "bug-fix",
        description="debug",
        body="body",
        reference=("safe.md", "safe"),
    )
    catalog = SkillCatalog.discover(repo)
    with pytest.raises(ValueError, match="unknown reference"):
        catalog.read_reference(
            "bug-fix",
            "../outside.md",
            max_chars=100,
        )


def test_eval_variant_maps_to_real_skill_architecture():
    assert _skills_enabled_for_variant("baseline_react") is False
    assert _skills_enabled_for_variant("planning") is False
    assert _skills_enabled_for_variant("planning_recovery") is False
    assert _planning_mode_for_variant("planning_recovery_skills") == "always"
    assert _recovery_mode_for_variant("planning_recovery_skills") == "structured"
    assert _skills_enabled_for_variant("planning_recovery_skills") is True
