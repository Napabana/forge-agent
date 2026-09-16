# Repo Map 增量索引与真实任务消融 TODO

状态更新时间：2026-09-16

当前基线：`dev`

## 当前状态

P2 生产主链已经完成：

```text
Repository State Detector
→ Changed Files
→ Persistent Structural Index (SQLite)
→ Query-aware Ranking
→ Token Budget Rendering
→ Model-visible Repo Map
```

当前状态：

| Milestone | 状态 | 证据 |
| --- | --- | --- |
| RM1 No / Static / Query-aware real-model baseline | DONE（small sample） | `repo_map_agent_ablation_real_v1`，4 cases × 4 variants × 1 run |
| RM2 Persistent per-file index | DONE | SQLite index + warm reuse + fallback |
| RM3 Changed-file incremental update | DONE | add/modify/delete/rename/staged/unstaged/untracked/HEAD + direct write update |
| RM4 Prompt Cache layout | PARTIAL | stable-prefix production layout 已完成；真实 Provider cache A/B harness 已就绪，尚未执行 |
| RM5 Incremental final ablation | DONE（small sample） | Incremental included；16-run real-model artifact 已冻结 |

## 已冻结的离线证据

12-case commit-history：

```text
MRR: 0.096954 → 0.318750
budget target recall: 0.364914 → 0.635251
```

Persistent strict equivalence：

```text
semantic / full ranking / visible-set / rendering = 12/12 equivalent
frozen Query-aware metric deltas = 0
```

5-run phase median：

```text
legacy build          0.6097s
persistent cold       0.9605s
warm load             0.2967s
query rerank          0.2080s
single-file update    0.1700s
two-file update       0.1766s
full rebuild          0.8045s
```

结论边界：收益来自 warm reuse / changed-file update；cold build 没有加速。
`71.26×` 仍只属于旧 reference-count 子步骤。

## 已冻结的 Real-model Small Sample

目录：

```text
evals/results/repo_map_agent_ablation_real_v1/
```

协议：

```text
model = deepseek-v4.1-flash
provider = openai-compatible
cases = 4
variants = 4
repetitions = 1
runs = 16
```

观察值：

| Variant | solved | verifier | mean total tokens | mean cached input | mean latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| No Repo Map | 3/4 | 4/4 | 65,055 | 26,048 | 70.45s |
| Static Repo Map | 0/4 | 4/4 | 96,704 | 39,776 | 96.97s |
| Query-aware | 3/4 | 4/4 | 67,349 | 26,752 | 70.40s |
| Incremental Query-aware | 4/4 | 4/4 | 59,541 | 21,728 | 64.71s |

解释：

- 16/16 verifier pass，说明四组都生成了满足 hidden verifier 的 patch。
- Static 0/4 solved 来自 completion/resource boundary，不等于 patch 全失败。
- 这是每 cell 只执行 1 次的 non-deterministic small sample，不计算稳定 pass@1。

机器校验：

```bash
python -m evals.verify_p2_repo_map_real
```

## Exploration telemetry 修正

历史字段 `files_read` / `first_target_read_step` 只识别 `file_read/file_view`，而真实模型频繁使用 shell 访问文件，因此不进入正式 P2 performance claim。

新增离线 Trace reader：

```bash
python -m evals.repo_map_trace_analysis
```

它保留 legacy metric，同时增加 explicit shell-path access。历史 raw 不重写。

## RM4：唯一剩余正式实验

真实 Provider 已经返回非零 `cached_input_tokens`，所以现在可以做 prompt-layout controlled A/B。

先跑一次：

```bash
python -m evals.repo_map_prompt_cache_ablation --config config/default.yaml --repetitions 1 --measured-calls 4 --max-tokens 64 --output ../forge-agent-evals/repo-map-prompt-cache-ab
```

该实验只比较：

```text
legacy_dynamic_first
vs
stable_prefix
```

保持 model、tool schemas、user message 和 dynamic context 形状一致。

只有该实验跑完并审计 provider usage 后，才能决定是否写 cached input fraction、uncached input tokens 或 provider-specific cache effect。

不能提前写“Prompt Cache 提升 X%”。

## 可选增强，不是 blocker

- [ ] Real-model A/B/C/D 做 3 repetitions，提高统计稳定性；
- [ ] 扩大到更多真实仓库任务；
- [ ] 换第二个 provider 验证 cache layout 是否具有 provider portability。

这些都不是当前 P2 persistent/incremental 实现的完成 blocker。
