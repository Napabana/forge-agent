---
name: experience-recovery-workflow-45e2c97c
description: "Use when a coding task hits a structured failure that requires bounded recovery and verification."
---
# Recovery Workflow

Use only when the current task matches the observed structured failure/recovery pattern.
Observed failure categories: no_progress, permission_denied, test_failure.
Observed recovery strategies: change_approach, inspect, replan.

Workflow:
1. Inspect the smallest relevant repository evidence before editing.
2. Use the smallest relevant tool action for the current step.
3. Inspect the smallest relevant repository evidence before editing.
4. Create or confirm a bounded execution plan before repository mutation.
5. Use shell commands only when repository evidence or verification requires them.
6. Run the narrowest relevant verification after the latest change.
7. Run the narrowest relevant verification after the latest change.
8. Inspect the smallest relevant repository evidence before editing.
9. Run the narrowest relevant verification after the latest change.
10. Revise the structured plan when the previous approach is invalidated.
11. Make the minimal repository change supported by the inspected evidence.
12. Run the narrowest relevant verification after the latest change.
13. Inspect the smallest relevant repository evidence before editing.
14. Revise the structured plan when the previous approach is invalidated.
15. Make the minimal repository change supported by the inspected evidence.
16. Inspect the smallest relevant repository evidence before editing.
17. Make the minimal repository change supported by the inspected evidence.
18. Inspect the smallest relevant repository evidence before editing.
19. Run the narrowest relevant verification after the latest change.
20. Finish only after the repository outcome and required verification are satisfied.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
