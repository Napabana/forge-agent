"""Candidate generation boundary. The default implementation is deterministic."""
from __future__ import annotations

from typing import Protocol

from experience.schema import ExperiencePattern, PatternType, SkillCandidate


class CandidateGenerator(Protocol):
    def generate(self, pattern: ExperiencePattern, *, skill_name: str | None = None) -> SkillCandidate: ...


_STEP_TEXT = {
    "PLAN": "Create or confirm a bounded execution plan before repository mutation.",
    "INSPECT": "Inspect the smallest relevant repository evidence before editing.",
    "EDIT": "Make the minimal repository change supported by the inspected evidence.",
    "TEST": "Run the narrowest relevant verification after the latest change.",
    "SHELL": "Use shell commands only when repository evidence or verification requires them.",
    "EXTERNAL_TOOL": "Use the relevant external capability only when it is required by the task.",
    "TOOL": "Use the smallest relevant tool action for the current step.",
    "REPLAN": "Revise the structured plan when the previous approach is invalidated.",
    "COMPLETION_REJECTED": "Resolve the unmet completion requirement before finishing again.",
    "FINISH": "Finish only after the repository outcome and required verification are satisfied.",
}


class DeterministicCandidateGenerator:
    """Render typed patterns into standard P2-3 SKILL.md-compatible instructions."""

    def generate(self, pattern: ExperiencePattern, *, skill_name: str | None = None) -> SkillCandidate:
        name = skill_name or f"experience-{pattern.pattern_type.value.replace('_', '-')}-{pattern.pattern_id[-8:]}"
        if pattern.pattern_type is PatternType.RECOVERY_WORKFLOW:
            description = "Use when a coding task hits a structured failure that requires bounded recovery and verification."
            lines = [
                "# Recovery Workflow",
                "",
                "Use only when the current task matches the observed structured failure/recovery pattern.",
            ]
            if pattern.failure_categories:
                lines.append(f"Observed failure categories: {', '.join(pattern.failure_categories)}.")
            if pattern.recovery_strategies:
                lines.append(f"Observed recovery strategies: {', '.join(pattern.recovery_strategies)}.")
            lines.extend(["", "Workflow:"])
        else:
            description = "Use for coding tasks that match this repeatedly observed verified repository workflow."
            lines = [
                "# Verified Repository Workflow",
                "",
                "Use only when the task matches the workflow; do not force this Skill onto unrelated tasks.",
                "",
                "Workflow:",
            ]

        numbered = 0
        for token in pattern.signature:
            if token.startswith("failure:") or token.startswith("recovery:") or token.startswith("FAIL:") or token.startswith("RECOVER:"):
                continue
            text = _STEP_TEXT.get(token)
            if text is None:
                continue
            numbered += 1
            lines.append(f"{numbered}. {text}")
        if numbered == 0:
            lines.append("1. Follow only the typed failure/recovery sequence recorded by the candidate provenance.")
        lines.extend([
            "",
            "Do not treat a successful edit as completion by itself.",
            "Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.",
        ])
        return SkillCandidate.build(
            skill_name=name,
            description=description,
            instructions="\n".join(lines),
            pattern=pattern,
        )
