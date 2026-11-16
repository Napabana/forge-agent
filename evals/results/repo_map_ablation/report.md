# Repo Map ablation

- Runner revision: `30036d5028ad904fbecc37eef05fe0e4fbbad0e1`
- Valid retrieval cases: `12`
- Skipped cases: `0`

## Retrieval quality

| variant | cases | R@1 | R@3 | R@5 | MRR | mean target rank | budget recall | mean map tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static | 12 | 0.0000 | 0.0000 | 0.0000 | 0.0970 | 30.292 | 0.3649 | 10098.0 |
| query_aware | 12 | 0.0833 | 0.2063 | 0.2561 | 0.3187 | 20.818 | 0.6353 | 10111.3 |

## Reference-scoring performance

- baseline: median `35.1176s`, p95 `35.8422s`, runs `5`
- optimized: median `0.4928s`, p95 `0.5501s`, runs `5`
- Speedup: `71.255x`
- Semantic equivalent: `True`
