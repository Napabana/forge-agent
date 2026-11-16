# Repo Map Real Coding Agent Ablation

- Execution status: `executed`
- Real model executed: `True`
- Cases: `4`
- Variants: `no_repo_map, static_repo_map, query_aware_repo_map, incremental_query_aware_repo_map`

## Observed small-sample results

| variant | runs | solved | verifier pass | mean input tokens | mean total tokens | mean latency (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| no_repo_map | 4 | 3 | 4 | 62551.8 | 65055.2 | 70.448 |
| static_repo_map | 4 | 0 | 4 | 92885.2 | 96704.2 | 96.968 |
| query_aware_repo_map | 4 | 3 | 4 | 64452.0 | 67349.2 | 70.400 |
| incremental_query_aware_repo_map | 4 | 4 | 4 | 57405.0 | 59541.0 | 64.714 |

These are observed Real-model Small Sample values, not stable pass@1 estimates.
