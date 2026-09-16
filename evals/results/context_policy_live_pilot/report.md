# Context Policy B1 Benchmark

- Runner revision: `80a60f3a1a0873640004abdea46f041cab4734c7`
- Fixture SHA256: `f9f57af5faead4715e52ba21ac247787107a3ad6622f8663759c7d34ab1ccece`
- Semantic mode: `live`

| variant | runs | pass rate | mean raw tokens | mean final tokens | compaction ratio | final pressure | summary calls | summary tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| hybrid_compaction | 3 | 0.667 | 4765.3 | 954.0 | 0.784 | 0.280 | 3 | 27060 |

## Contract failures

- `resume-long-session` / `hybrid_compaction`: current_user_not_exactly_once
