# B2 Context Policy Agent Ablation

- runner revision: `ad5546b619916c3b8c16ca2cd21868a6b56368e3`
- fixture sha256: `f47979744e68583074aa3f7ad01a3ef5b9af3f7828b82ea659886fa9b5d5ac4d`
- model: `deepseek-v4.1-flash`
- runs: 1
- cases: long-hard-constraint

| variant | pass@1 | verifier pass | max-step exhausted | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid_compaction | 1.000 | 1.000 | 0.000 | 86685.0 | 86685.0 | 97.1 | 1.000 | 1 | 1.136 |

## Per-run evidence

| case | variant | strict | verifier | status | termination | resource | input | output | total | latency(s) | rejections | diff | trace |
|---|---|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|
| long-hard-constraint | hybrid_compaction | 1 | passed | success | completion_satisfied | - | 78273 | 1779 | 86685 | 97.1 | 0 | yes | `/mnt/e/2806/forgeAgent/forge-agent/evals/results/context_policy_agent_ablation_v3_targeted/long-hard-constraint_hybrid/traces/hybrid_compaction/long-hard-constraint/b2-hybrid_compaction-long-hard-constraint_20260916_065552.jsonl` |

## B2 v2 → v3 cell comparison

| case | variant | v2 strict | v3 strict | v2 verifier | v3 verifier | v2 status | v3 status | v3 termination/resource |
|---|---|---:|---:|---|---|---|---|---|
| long-hard-constraint | hybrid_compaction | False | True | passed | passed | failed | success | completion_satisfied / - |

## Interpretation limits

This experiment has only three cases per variant (`n=3`) and uses a non-deterministic model. The results are descriptive evidence for these runs, not a general performance claim.
