---
name: test-and-verify
description: Use when a coding task requires tests, regression verification, or final-state validation.
---
# Test and Verify Workflow

1. Determine the narrow test that exercises the changed behavior.
2. Run verification after the implementation change.
3. If any repository file changes after a passing test, verify again.
4. Treat failing tests as evidence to diagnose, not as completion.
