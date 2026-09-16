# Context Policy B1 Benchmark

- Runner revision: `eccf8991ff49bec651c7ac9ba829aa7d5b3bc056`
- Fixture SHA256: `f9f57af5faead4715e52ba21ac247787107a3ad6622f8663759c7d34ab1ccece`
- Semantic mode: `fixture`

| variant | runs | pass rate | mean raw tokens | mean final tokens | compaction ratio | final pressure | summary calls | summary tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| budget_trim_only | 7 | 0.714 | 6080.9 | 1529.0 | 0.716 | 0.445 | 0 | 0 |
| deterministic_pruning | 7 | 0.857 | 6080.9 | 1588.1 | 0.707 | 0.462 | 0 | 0 |
| hybrid_compaction | 7 | 1.000 | 6080.9 | 901.1 | 0.834 | 0.265 | 8 | 0 |

## Contract failures

- `repeated-compaction` / `budget_trim_only`: recent_raw_marker_lost
- `dirty-repo-revision` / `budget_trim_only`: recent_raw_marker_lost
- `dirty-repo-revision` / `deterministic_pruning`: recent_raw_marker_lost
