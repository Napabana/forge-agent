---
name: experience-recovery-workflow-cbbf464f
description: "Use when a coding task hits a structured failure matching this observed recovery motif."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `tool_failure`.
Observed recovery strategy: `inspect`.
Source evidence: 2 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Inspect the failure evidence and surrounding repository state before changing code or retrying.
3. Inspect the smallest relevant repository evidence before editing.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
