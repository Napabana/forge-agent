# Persistent / Incremental Repo Map Benchmark

- Runner revision: `840f158f10b7cda6298870532ee6fdf5aef11598`
- Retrieval cases: `12`
- Performance runs: `5`

## Frozen retrieval protocol

- Semantic/ranking/rendering equivalent on all cases: `True`
- MRR: `0.318750`
- Budget target recall: `0.635251`
- Matches frozen Query-aware report: `True`

## Runtime phases

| phase | median (s) | p95 (s) |
| --- | ---: | ---: |
| legacy_build_seconds | 0.609702 | 0.625060 |
| cold_build_seconds | 0.960537 | 0.975744 |
| warm_load_seconds | 0.296724 | 0.301836 |
| query_rerank_seconds | 0.207989 | 0.214367 |
| single_file_update_seconds | 0.170006 | 0.175353 |
| multi_file_update_seconds | 0.176620 | 0.181662 |
| full_rebuild_seconds | 0.804537 | 0.805182 |

- Warm start reparsed zero files: `True`
- Single-file update parsed only one file: `True`
- Multi-file update parsed only two files: `True`
- Semantic equivalent: `True`
- Working tree clean after benchmark: `True`

These timings are Repo Map phases on the recorded machine, not end-to-end Agent latency.
