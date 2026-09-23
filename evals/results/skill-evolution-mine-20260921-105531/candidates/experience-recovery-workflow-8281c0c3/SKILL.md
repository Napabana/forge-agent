---
name: experience-recovery-workflow-8281c0c3
description: "Use when a coding task hits a structured failure that requires bounded recovery and verification."
---
# Recovery Workflow

Use only when the current task matches the observed structured failure/recovery pattern.
Observed failure categories: no_progress, tool_failure.
Observed recovery strategies: change_approach, inspect.

Workflow:
1. Inspect the smallest relevant repository evidence before editing.
2. Inspect the smallest relevant repository evidence before editing.
3. Inspect the smallest relevant repository evidence before editing.
4. Create or confirm a bounded execution plan before repository mutation.
5. Inspect the smallest relevant repository evidence before editing.
6. Make the minimal repository change supported by the inspected evidence.
7. Use the smallest relevant tool action for the current step.
8. Inspect the smallest relevant repository evidence before editing.
9. Make the minimal repository change supported by the inspected evidence.
10. Run the narrowest relevant verification after the latest change.
11. Use the smallest relevant tool action for the current step.
12. Inspect the smallest relevant repository evidence before editing.
13. Finish only after the repository outcome and required verification are satisfied.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
