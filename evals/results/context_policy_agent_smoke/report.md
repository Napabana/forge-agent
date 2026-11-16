# B2 Context Policy Agent Ablation

- runner revision: `9e2910f37284c4ea2c390cf9d7eab4c632e59aa2`
- fixture sha256: `1f0f29f3ed63e9a7d22f4fd586f3ab17cae9d2eeac59e6e43c3fb911cc56eadc`
- model: `deepseek-v4.1-flash`
- runs: 1
- cases: long-hard-constraint

| variant | pass@1 | tokens/solved | mean total tokens | mean latency(s) | trigger rate | semantic calls | max pressure |
|---|---:|---:|---:|---:|---:|---:|---:|
| hybrid_compaction | 1.000 | 36870.0 | 36870.0 | 53.4 | 1.000 | 1 | 0.974 |
