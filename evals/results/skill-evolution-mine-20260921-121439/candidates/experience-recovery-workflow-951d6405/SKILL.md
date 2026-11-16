---
name: experience-recovery-workflow-951d6405
description: "Use after structured recovery classifies test_failure and selects inspect; the observed next semantic action is TEST."
---
# Recovery Motif

Use only when the current structured failure classification matches this observed motif.
Observed failure category: `test_failure`.
Observed recovery strategy: `inspect`.
Source evidence: 1 accepted trajectory/trajectories.

Recovery:
1. Do not retry the failed action unchanged.
2. Inspect the failure evidence and surrounding repository state before changing code or retrying.
3. Run the narrowest relevant verification after the latest change.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
