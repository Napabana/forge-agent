# B2 Context Policy Agent Ablation

- runner revision: `ad5546b619916c3b8c16ca2cd21868a6b56368e3`
- fixture sha256: `f47979744e68583074aa3f7ad01a3ef5b9af3f7828b82ea659886fa9b5d5ac4d`
- model: `deepseek-v4.1-flash`
- runs: 1
- cases: superseded-state

| variant | pass@1 | verifier pass | max-step exhausted | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid_compaction | 1.000 | 1.000 | 0.000 | 67451.0 | 67451.0 | 59.3 | 1.000 | 1 | 0.975 |

## Per-run evidence

| case | variant | strict | verifier | status | termination | resource | input | output | total | latency(s) | rejections | diff | trace |
|---|---|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|
| superseded-state | hybrid_compaction | 1 | passed | success | completion_satisfied | - | 59932 | 2159 | 67451 | 59.3 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3_targeted/superseded-state_hybrid/traces/hybrid_compaction/superseded-state/b2-hybrid_compaction-superseded-state_20260916_070111.jsonl` |

## B2 v2 → v3 cell comparison

| case | variant | v2 strict | v3 strict | v2 verifier | v3 verifier | v2 status | v3 status | v3 termination/resource |
|---|---|---:|---:|---|---|---|---|---|
| superseded-state | hybrid_compaction | False | True | passed | passed | max_steps | success | completion_satisfied / - |

## Interpretation limits

This experiment has only three cases per variant (`n=3`) and uses a non-deterministic model. The results are descriptive evidence for these runs, not a general performance claim.
