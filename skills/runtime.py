"""Runtime progressive disclosure for Agent Skills.

Skill controls only change model context. They never execute scripts or external
capabilities; executable actions remain normal ToolExecutor calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.task import EventType
from llm.base import LLMToolSchema
from skills.catalog import SkillCatalog

SKILL_LOAD = "skill_load"
SKILL_REFERENCE_LOAD = "skill_reference_load"
SKILL_CONTROL_NAMES = frozenset({SKILL_LOAD, SKILL_REFERENCE_LOAD})


@dataclass(frozen=True)
class SkillControlResult:
    accepted: bool
    message: str
    event_type: EventType
    payload: dict[str, Any]


def skill_control_schemas(catalog: SkillCatalog) -> tuple[LLMToolSchema, ...]:
    if not catalog.names:
        return ()
    return (
        LLMToolSchema(
            name=SKILL_LOAD,
            description=(
                "Load the full instructions for one relevant Agent Skill after reviewing "
                "the metadata catalog. This changes runtime context only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": list(catalog.names)},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        LLMToolSchema(
            name=SKILL_REFERENCE_LOAD,
            description=(
                "Load one text reference declared by an already-loaded Agent Skill. "
                "This does not execute scripts or external capabilities."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": list(catalog.names)},
                    "reference": {"type": "string"},
                },
                "required": ["skill", "reference"],
                "additionalProperties": False,
            },
        ),
    )


class SkillRuntime:
    def __init__(
        self,
        catalog: SkillCatalog,
        *,
        enabled: bool,
        max_loaded: int = 3,
        skill_max_chars: int = 12_000,
        reference_max_chars: int = 8_000,
    ) -> None:
        if max_loaded < 1:
            raise ValueError("skills max_loaded must be >= 1")
        if skill_max_chars < 1 or reference_max_chars < 1:
            raise ValueError("skill context limits must be positive")
        self.catalog = catalog
        self.enabled = bool(enabled)
        self.max_loaded = int(max_loaded)
        self.skill_max_chars = int(skill_max_chars)
        self.reference_max_chars = int(reference_max_chars)
        self._loaded: dict[str, str] = {}
        self._references: dict[tuple[str, str], str] = {}

    @property
    def loaded_names(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def schemas(self) -> tuple[LLMToolSchema, ...]:
        if not self.enabled:
            return ()
        return skill_control_schemas(self.catalog)

    def is_control(self, name: str) -> bool:
        return self.enabled and name in SKILL_CONTROL_NAMES

    def render_context(self) -> str:
        if not self.enabled or len(self.catalog) == 0:
            return ""
        lines = [
            "[Agent Skills]",
            "Available skills (metadata only; full instructions are not loaded yet):",
        ]
        for skill in self.catalog.skills:
            state = "loaded" if skill.name in self._loaded else "metadata-only"
            lines.append(
                f"- {skill.name}: {skill.description} "
                f"[{state}; source={skill.source}; refs={len(skill.references)}; "
                f"scripts={len(skill.scripts)}]"
            )
        lines.append(
            "Load a skill only when its description is relevant. Do not load every skill. "
            "Use skill_load for full instructions and skill_reference_load only for a "
            "specific declared reference. Skill scripts are never auto-executed."
        )
        for content in self._loaded.values():
            lines.extend(["", content])
        for (skill_name, reference), content in self._references.items():
            lines.extend([
                "",
                f"### Loaded Skill Reference: {skill_name}/references/{reference}",
                content,
            ])
        return "\n".join(lines)

    def apply_control(self, name: str, params: dict[str, Any]) -> SkillControlResult:
        try:
            if name == SKILL_LOAD:
                return self._load(params)
            if name == SKILL_REFERENCE_LOAD:
                return self._load_reference(params)
            raise ValueError(f"unknown skill control: {name}")
        except (OSError, TypeError, ValueError) as exc:
            hint = ""
            if name == SKILL_LOAD:
                available = ", ".join(self.catalog.names) or "none"
                hint = (
                    " Expected skill_load params: provide a non-empty name from the "
                    f"available catalog: {available}."
                )
            return SkillControlResult(
                accepted=False,
                message=f"[SKILL CONTROL REJECTED] {exc}.{hint}",
                event_type=EventType.SKILL_REJECTED,
                payload={"control": name, "error": str(exc)},
            )

    def _load(self, params: dict[str, Any]) -> SkillControlResult:
        name = str(params.get("name", "")).strip()
        skill = self.catalog.get(name)
        if skill is None:
            raise ValueError(f"unknown skill: {name}")
        if name not in self._loaded and len(self._loaded) >= self.max_loaded:
            raise ValueError(
                f"loaded skill limit reached ({self.max_loaded}); do not load every skill"
            )
        already_loaded = name in self._loaded
        self._loaded[name] = skill.render(max_chars=self.skill_max_chars)
        return SkillControlResult(
            accepted=True,
            message=f"[SKILL] Loaded {name}. Follow its instructions when relevant.",
            event_type=EventType.SKILL_LOADED,
            payload={
                "skill": name,
                "source": skill.source,
                "reference_count": len(skill.references),
                "script_count": len(skill.scripts),
                "already_loaded": already_loaded,
            },
        )

    def _load_reference(self, params: dict[str, Any]) -> SkillControlResult:
        skill_name = str(params.get("skill", "")).strip()
        reference = str(params.get("reference", "")).strip()
        if skill_name not in self._loaded:
            raise ValueError(
                f"skill {skill_name!r} must be loaded before loading its references"
            )
        key = (skill_name, reference)
        self._references[key] = self.catalog.read_reference(
            skill_name,
            reference,
            max_chars=self.reference_max_chars,
        )
        return SkillControlResult(
            accepted=True,
            message=f"[SKILL] Loaded reference {skill_name}/references/{reference}.",
            event_type=EventType.SKILL_REFERENCE_LOADED,
            payload={"skill": skill_name, "reference": reference},
        )
