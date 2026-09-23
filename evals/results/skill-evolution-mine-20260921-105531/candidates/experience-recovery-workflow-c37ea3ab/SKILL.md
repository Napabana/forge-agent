---
name: experience-recovery-workflow-c37ea3ab
description: "Use when a coding task hits a structured failure that requires bounded recovery and verification."
---
# Recovery Workflow

Use only when the current task matches the observed structured failure/recovery pattern.
Observed failure categories: no_progress, tool_failure.
Observed recovery strategies: change_approach, inspect.

Workflow:
1. Use shell commands only when repository evidence or verification requires them.
2. Inspect the smallest relevant repository evidence before editing.
3. Inspect the smallest relevant repository evidence before editing.
4. Inspect the smallest relevant repository evidence before editing.
5. Use the smallest relevant tool action for the current step.
6. Create or confirm a bounded execution plan before repository mutation.
7. Make the minimal repository change supported by the inspected evidence.
8. Run the narrowest relevant verification after the latest change.
9. Use the smallest relevant tool action for the current step.
10. Finish only after the repository outcome and required verification are satisfied.

Do not treat a successful edit as completion by itself.
Keep normal Forge permission, tool, acceptance, and verification boundaries unchanged.
