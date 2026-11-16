---
name: experience-recovery-workflow-e79ca097
description: "Use after structured recovery classifies no_progress and selects change_approach; the observed next semantic action is PLAN."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `no_progress`.
Observed recovery strategy: `change_approach`.
Source evidence: 2 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Change approach instead of repeating the action that produced no semantic progress.
3. Create or confirm a bounded execution plan before repository mutation.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
