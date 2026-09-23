---
name: experience-recovery-workflow-c5f3f3b0
description: "Use after structured recovery classifies permission_denied and selects change_approach; the observed next semantic action is TEST."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `permission_denied`.
Observed recovery strategy: `change_approach`.
Source evidence: 1 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Change approach instead of repeating the action that produced no semantic progress.
3. Run the narrowest relevant verification after the latest change.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
