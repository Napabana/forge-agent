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


_RECOVERY_STRATEGY_TEXT = {
    "inspect": "Inspect the failure evidence and surrounding repository state before changing code or retrying.",
    "change_approach": "Change approach instead of repeating the action that produced no semantic progress.",
    "replan": "Revise the structured plan using the failure evidence before continuing.",
    "rerun_test": "Re-run the relevant verification only after the required corrective step is complete.",
}


class DeterministicCandidateGenerator:
    """Render typed patterns into standard P2-3 SKILL.md-compatible instructions."""

    def generate(self, pattern: ExperiencePattern, *, skill_name: str | None = None) -> SkillCandidate:
        name = skill_name or f"experience-{pattern.pattern_type.value.replace('_', '-')}-{pattern.pattern_id[-8:]}"
        if pattern.pattern_type is PatternType.RECOVERY_WORKFLOW:
            description = "Use when a coding task hits a structured failure matching this observed recovery motif."
            failure = pattern.failure_categories[0] if pattern.failure_categories else "unknown"
            recovery = pattern.recovery_strategies[0] if pattern.recovery_strategies else "unknown"
            lines = [
                "# Recovery Motif",
                "",
                "Use only when the current structured failure classification matches this observed motif.",
                f"Observed failure category: `{failure}`.",
                f"Observed recovery strategy: `{recovery}`.",
                f"Source evidence: {pattern.evidence_count} accepted trajectory/trajectories.",
                "",
                "Recovery:",
                "1. Do not retry the failed action unchanged.",
                "2. " + _RECOVERY_STRATEGY_TEXT.get(
                    recovery,
                    f"Apply the recorded `{recovery}` recovery strategy before continuing.",
                ),
            ]
            numbered = 2
            for token in pattern.signature:
                if token.startswith(("failure:", "recovery:", "FAIL:", "RECOVER:")):
                    continue
                text = _STEP_TEXT.get(token)
                if text is None:
                    continue
                numbered += 1
                lines.append(f"{numbered}. {text}")
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
                text = _STEP_TEXT.get(token)
                if text is None:
                    continue
                numbered += 1
                lines.append(f"{numbered}. {text}")
            if numbered == 0:
                lines.append("1. Follow only the typed workflow recorded by the candidate provenance.")
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
