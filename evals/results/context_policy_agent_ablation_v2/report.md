# B2 Context Policy Agent Ablation

- runner revision: `16e0741c057ddb3f9e3a78291294686c36e10696`
- fixture sha256: `f47979744e68583074aa3f7ad01a3ef5b9af3f7828b82ea659886fa9b5d5ac4d`
- model: `deepseek-v4.1-flash`
- runs: 9
- cases: long-hard-constraint, huge-tool-history, superseded-state

| variant | pass@1 | verifier pass | max-step exhausted | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 0.000 | 0.667 | 0.667 | 0.0 | 123167.0 | 271.8 | 0.000 | 0 | n/a |
| pruning_only | 0.333 | 0.667 | 0.667 | 342351.0 | 114117.0 | 264.9 | 0.000 | 0 | 1.749 |
| hybrid_compaction | 0.333 | 1.000 | 0.333 | 283332.0 | 94444.0 | 87.9 | 1.000 | 4 | 1.915 |
