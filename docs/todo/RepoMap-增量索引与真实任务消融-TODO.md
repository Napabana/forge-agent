# Repo Map 增量索引与真实任务消融 TODO

状态更新时间：2026-09-16

当前基线：`dev`

## 最终状态

P2 已完成并收口，状态：**DONE**。

生产主链：

```text
Repository State Detector
→ Changed Files
→ Persistent Structural Index (SQLite)
→ Query-aware Ranking
→ Token Budget Rendering
→ Model-visible Repo Map
```

Milestone：

| Milestone | 状态 | 证据 |
| --- | --- | --- |
| RM1 No / Static / Query-aware real-model baseline | DONE（small sample） | `repo_map_agent_ablation_real_v1`，4 cases × 4 variants × 1 run |
| RM2 Persistent per-file index | DONE | SQLite index + warm reuse + corruption/schema fallback |
| RM3 Changed-file incremental update | DONE | add/modify/delete/rename/staged/unstaged/untracked/HEAD + direct write update |
| RM4 Prompt Cache layout | DONE（structural only） | stable-prefix production layout + deterministic regression；不形成定量 cache improvement claim |
| RM5 Incremental final ablation | DONE（small sample） | Incremental included；16-run real-model artifact 已冻结 |

## 冻结离线证据

12-case commit-history retrieval：

```text
MRR:                  0.096954 → 0.318750
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

结论边界：收益来自 warm reuse / changed-file update；cold build 没有加速。`71.26×` 仍只属于旧 reference-count 子步骤，不是完整 Repo Map 或 Agent E2E speedup。

## Real-model Small Sample

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
- 每 cell 只执行 1 次，属于 non-deterministic small sample，不计算稳定 pass@1，不外推总体 success rate。

机器校验：

```bash
python -m evals.verify_p2_repo_map_real
```

## Exploration telemetry

历史 `files_read` / `first_target_read_step` 只识别 `file_read/file_view`，而真实模型频繁使用 shell 访问文件，因此不进入正式 P2 performance claim。

新增离线 Trace reader：

```text
evals/repo_map_trace_analysis.py
```

它保留 legacy metric，同时增加 explicit shell-path access；历史 raw 不重写。

## RM4 Prompt Cache 最终结论

生产布局：

```text
stable system rules
→ stable textual tool descriptions / tool schemas
→ dynamic Repo Map
→ conversation
```

该顺序作为结构优化保留，目标是让支持 prefix caching 的 Provider 尽可能复用更长的稳定前缀。

已执行一次 controlled real-provider A/B：

```text
legacy_dynamic_first:
mean input = 4514
mean cached input = 0
cache fraction = 0.0

stable_prefix:
mean input = 4514
mean cached input = 0
cache fraction = 0.0
```

两组都没有观察到 provider-reported cached input，因此该实验不提供 layout cache effect 的定量证据。

同时真实 Agent Trace 中曾观察到非零 provider-reported cached tokens，所以不能把本次 A/B 的 0 解读为 Provider 不支持缓存或 Forge 无法解析缓存。

最终决定：

- 保留 stable-prefix 生产布局；
- 保留 layout regression 和 cache A/B harness；
- 不写 Prompt Cache 提升百分比；
- 不再追加 identical-control、更多重复或第二 Provider 实验；
- RM4 记为 **DONE（structural optimization, no quantitative provider-cache claim）**。

## 可引用表述

可以说：

> Query-aware Repo Map 在 12-case frozen commit-history benchmark 上将 MRR 从 0.097 提升到 0.319、Token Budget 内 target recall 从 0.365 提升到 0.635；随后将 Repo Map 拆为持久化 SQLite 结构索引与 Query-aware 视图，Query 改变只 rerank、代码修改按 changed file 增量更新，并用 12-case strict equivalence regression 保证旧排序/渲染语义不变。

追问真实 Agent 时可以补充：

> 在 4-case × 4-variant 的 single-run real-model small sample 中，四组 patch 均通过 hidden verifier；Incremental Query-aware 观察到 4/4 completion、mean total tokens 约 59.5k、mean latency 约 64.7s。

Prompt Cache 只说：

> 将稳定 rules/tool descriptions 放在动态 Repo Map 前，尽量扩大支持 prefix caching Provider 的可复用前缀；没有宣称定量 cache 提升。

## 不可夸大

- 不写“Repo Map 让 Agent 总体成功率提升 X%”；
- 不写“Incremental 稳定优于所有方案”；
- 不写“Static Repo Map 代码成功率为 0%”；
- 不写“整个 Agent 快 71×”；
- 不写“Prompt Cache 命中率提升 X%”；
- 不把 3.85s → 3.10s 的 microbenchmark latency 当作 layout 提速结论。

## 后续

P2 没有剩余 blocker。

以下只属于未来可选研究，不再作为当前 TODO：更多 repetitions、更大真实任务集、第二 Provider cache portability、进一步 Repo Map retrieval/ranking 研究。
