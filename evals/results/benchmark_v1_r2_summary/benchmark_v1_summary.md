# Forge Agent Real-model Benchmark V1

Benchmark ID: forge-agent-real-model-benchmark-v1-r2
Suite: forge-agent-benchmark-v1-r2
Suite SHA-256: f7ba1370fe1442773967ec4f65883be5cdc0a21f14ced28d1ea57ed1f384bd0a
Tasks x repetitions x variants: 12 x 2 x 2 = 48
Source repository: Napabana/pr-test @ 23019998f2e801e79dea59fd23fc49c58fc20038
Provider / model: openai / deepseek-v4.1-flash

## Primary metric

| Variant | Observed successes | Trials | Observed success rate |
| --- | ---: | ---: | ---: |
| baseline_react | 23 | 24 | 95.8% |
| planning_recovery_skills | 23 | 24 | 95.8% |

Absolute delta: +0.0 percentage points.

## Successful-trial efficiency

| Variant | Steps mean / median | Tokens mean / median | Wall time mean / median (s) |
| --- | ---: | ---: | ---: |
| baseline_react | 12.30 / 11.00 | 169602.0 / 119874.0 | 102.02 / 92.97 |
| planning_recovery_skills | 15.65 / 16.00 | 223880.9 / 169316.0 | 118.32 / 105.91 |

## Recovery and Skills subgroups

| Variant | Recovery success | Skill trigger loaded | Skill false-trigger |
| --- | ---: | ---: | ---: |
| baseline_react | 6/6 (100.0%) | 0/20 (0.0%) | 0/4 (0.0%) |
| planning_recovery_skills | 6/6 (100.0%) | 1/20 (5.0%) | 0/4 (0.0%) |

## Paired outcomes

Full-P2 wins: 1, losses: 1, ties: 22.

## Per-task comparison

| Task | Baseline | Full P2 | Delta |
| --- | ---: | ---: | ---: |
| subtract-regression-recovery | 2/2 | 2/2 | +0.0 pp |
| batch-boundary-recovery | 2/2 | 2/2 | +0.0 pp |
| divide-float-regression-recovery | 2/2 | 2/2 | +0.0 pp |
| square-operation-feature | 2/2 | 2/2 | +0.0 pp |
| registry-contains-feature | 2/2 | 2/2 | +0.0 pp |
| runtime-policy-mode-live | 2/2 | 2/2 | +0.0 pp |
| power-operation-integration | 2/2 | 1/2 | -50.0 pp |
| registry-case-insensitive | 2/2 | 2/2 | +0.0 pp |
| policy-deny-list | 2/2 | 2/2 | +0.0 pp |
| batch-stop-on-error | 1/2 | 2/2 | +50.0 pp |
| custom-registry-preserve-overrides | 2/2 | 2/2 | +0.0 pp |
| multiply-completion-guard | 2/2 | 2/2 | +0.0 pp |

## Claim boundary

Observed outcomes from one frozen, project-authored 12-task suite with two repetitions per architecture variant. They are not a stable population pass@1 estimate, do not use an LLM judge, and should not be generalized beyond this benchmark without additional evaluation. Successful-only efficiency metrics exclude failed trials; all-trial audit metrics are retained separately.
