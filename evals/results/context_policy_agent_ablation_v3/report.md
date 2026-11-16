# B2 Context Policy Agent Ablation

- runner revision: `ad5546b619916c3b8c16ca2cd21868a6b56368e3`
- fixture sha256: `f47979744e68583074aa3f7ad01a3ef5b9af3f7828b82ea659886fa9b5d5ac4d`
- model: `deepseek-v4.1-flash`
- runs: 9
- cases: long-hard-constraint, huge-tool-history, superseded-state

| variant | pass@1 | verifier pass | max-step exhausted | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 0.333 | 0.667 | 0.667 | 369072.0 | 123024.0 | 267.3 | 0.000 | 0 | n/a |
| pruning_only | 0.667 | 0.667 | 0.333 | 150888.5 | 100592.3 | 250.9 | 0.000 | 0 | 1.720 |
| hybrid_compaction | 0.667 | 1.000 | 0.333 | 93236.5 | 62157.7 | 108.0 | 1.000 | 5 | 1.870 |

## Per-run evidence

| case | variant | strict | verifier | status | termination | resource | input | output | total | latency(s) | rejections | diff | trace |
|---|---|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|
| long-hard-constraint | baseline | 0 | failed | incomplete | resource_exhausted | max_steps | 189643 | 2436 | 192079 | 586.8 | 0 | no | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/baseline/long-hard-constraint/b2-baseline-long-hard-constraint_20260916_070250.jsonl` |
| long-hard-constraint | pruning_only | 0 | failed | incomplete | resource_exhausted | max_steps | 150731 | 2258 | 152989 | 581.6 | 0 | no | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/pruning_only/long-hard-constraint/b2-pruning_only-long-hard-constraint_20260916_071240.jsonl` |
| long-hard-constraint | hybrid_compaction | 1 | passed | success | completion_satisfied | - | 30017 | 1112 | 37632 | 75.3 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/hybrid_compaction/long-hard-constraint/b2-hybrid_compaction-long-hard-constraint_20260916_072225.jsonl` |
| huge-tool-history | baseline | 1 | passed | success | completion_satisfied | - | 29253 | 1341 | 30594 | 58.1 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/baseline/huge-tool-history/b2-baseline-huge-tool-history_20260916_072343.jsonl` |
| huge-tool-history | pruning_only | 1 | passed | success | completion_satisfied | - | 38769 | 1777 | 40546 | 46.8 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/pruning_only/huge-tool-history/b2-pruning_only-huge-tool-history_20260916_072444.jsonl` |
| huge-tool-history | hybrid_compaction | 0 | passed | incomplete | resource_exhausted | max_steps | 74103 | 2558 | 77882 | 120.0 | 1 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/hybrid_compaction/huge-tool-history/b2-hybrid_compaction-huge-tool-history_20260916_072534.jsonl` |
| superseded-state | baseline | 0 | passed | incomplete | resource_exhausted | max_steps | 143435 | 2964 | 146399 | 157.1 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/baseline/superseded-state/b2-baseline-superseded-state_20260916_072738.jsonl` |
| superseded-state | pruning_only | 1 | passed | success | completion_satisfied | - | 105959 | 2283 | 108242 | 124.2 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/pruning_only/superseded-state/b2-pruning_only-superseded-state_20260916_073018.jsonl` |
| superseded-state | hybrid_compaction | 1 | passed | success | completion_satisfied | - | 49668 | 1932 | 70959 | 128.6 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3/traces/hybrid_compaction/superseded-state/b2-hybrid_compaction-superseded-state_20260916_073225.jsonl` |

## B2 v2 → v3 cell comparison

| case | variant | v2 strict | v3 strict | v2 verifier | v3 verifier | v2 status | v3 status | v3 termination/resource |
|---|---|---:|---:|---|---|---|---|---|
| long-hard-constraint | baseline | False | False | failed | failed | max_steps | incomplete | resource_exhausted / max_steps |
| long-hard-constraint | pruning_only | False | False | failed | failed | max_steps | incomplete | resource_exhausted / max_steps |
| long-hard-constraint | hybrid_compaction | False | True | passed | passed | failed | success | completion_satisfied / - |
| huge-tool-history | baseline | False | True | passed | passed | failed | success | completion_satisfied / - |
| huge-tool-history | pruning_only | True | True | passed | passed | success | success | completion_satisfied / - |
| huge-tool-history | hybrid_compaction | True | False | passed | passed | success | incomplete | resource_exhausted / max_steps |
| superseded-state | baseline | False | False | passed | passed | max_steps | incomplete | resource_exhausted / max_steps |
| superseded-state | pruning_only | False | True | passed | passed | max_steps | success | completion_satisfied / - |
| superseded-state | hybrid_compaction | False | True | passed | passed | max_steps | success | completion_satisfied / - |

## B2 findings

- Commit-aware completion: `long-hard-constraint/hybrid_compaction` and `huge-tool-history/baseline` changed from v2 verifier-passed guard failures to v3 SUCCESS.
- Superseded state: hybrid and pruning finish successfully in v3, while baseline still has verifier passed with `INCOMPLETE + resource_exhausted/max_steps`.
- Completion rejection: `huge-tool-history/hybrid_compaction` recorded one `LATEST_TEST_FAILED` rejection, continued, then ended `INCOMPLETE/max_steps`; it was not counted as strict success.

## Interpretation limits

This experiment has only three cases per variant (`n=3`) and uses a non-deterministic model. The results are descriptive evidence for these runs, not a general performance claim.
