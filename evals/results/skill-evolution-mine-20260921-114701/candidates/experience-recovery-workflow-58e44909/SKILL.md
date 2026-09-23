---
name: experience-recovery-workflow-58e44909
description: "Use when a coding task hits a structured failure matching this observed recovery motif."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `test_failure`.
Observed recovery strategy: `replan`.
Source evidence: 1 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Revise the structured plan using the failure evidence before continuing.
3. Inspect the smallest relevant repository evidence before editing.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
