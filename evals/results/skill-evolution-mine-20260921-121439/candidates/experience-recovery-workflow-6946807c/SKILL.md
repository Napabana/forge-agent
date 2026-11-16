---
name: experience-recovery-workflow-6946807c
description: "Use after structured recovery classifies test_failure and selects replan; the observed next semantic action is REPLAN."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `test_failure`.
Observed recovery strategy: `replan`.
Source evidence: 1 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Revise the structured plan using the failure evidence before continuing.
3. Revise the structured plan when the previous approach is invalidated.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
