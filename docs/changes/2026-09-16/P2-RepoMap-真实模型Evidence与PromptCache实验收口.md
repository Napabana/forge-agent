# P2 Repo Map：真实模型 Evidence 与 Prompt Cache 实验收口

日期：2026-09-16

## 背景

用户本地原计划只跑 smoke，但命令换行失效后实际执行了完整 `4 cases × 4 variants × 1 repetition = 16` 个真实模型 Agent run，并将结果 push 到 `dev`。

本轮不重跑付费 Agent 实验，直接审计并冻结现有证据。

## Real-model 结果

模型：`deepseek-v4.1-flash`。

四组 16 个 patch 全部通过 hidden verifier。Agent completion 观察值：

```text
No Repo Map              3/4
Static Repo Map          0/4
Query-aware Repo Map     3/4
Incremental Query-aware  4/4
```

Static 的 0/4 是 completion/resource boundary：patch 本身 4/4 verifier pass，因此不得表述为“Static 代码成功率 0%”。

Incremental Query-aware 本次观察值：

```text
mean total tokens = 59,541
mean latency       = 64.71s
verifier           = 4/4
completion         = 4/4
```

仅属于 4-case、单次/cell 的 Real-model Small Sample。

## 历史 Evidence 保护

`AGENTS.md` 要求不覆盖历史 `evals/results`。因此：

- 原 `evals/results/repo_map_agent_ablation/` 顶层 provider-free `not_executed/rows=0` artifact 恢复；
- 本次真实结果原样冻结到 `evals/results/repo_map_agent_ablation_real_v1/`；
- 已提交 Trace/fixture repos 不删除，real-v1 raw 中原 trace_path 仍可追溯到它们。

## Exploration metric

历史 harness 的 `files_read` 与 `first_target_read_step` 只统计 `file_read/file_view`，无法覆盖 shell `cat/grep/python` 等访问方式。

新增 `evals/repo_map_trace_analysis.py`，从 Trace v2 离线重算 explicit path access；不修改历史 raw。

## Prompt Cache 结构优化

生产 `agent/prompt.py` 保留稳定前缀布局：

```text
stable system rules
→ stable textual tool descriptions / tool schemas
→ dynamic Repo Map
→ conversation
```

设计目标是：对于按前缀匹配复用 prompt 的 Provider，让更长的稳定前缀位于动态仓库上下文之前。

这个结论只作为结构设计原则，不直接等于 Provider cache 命中率提升。

## Prompt Cache 真实 A/B 结果

新增并执行了：

```text
evals/repo_map_prompt_cache_ablation.py
```

协议：

```text
provider = openai-compatible
model = deepseek-v4.1-flash
repetitions = 1
warmup = 1 / variant
measured calls = 4 / variant
max output tokens = 64
```

唯一变量是 system prompt 中 dynamic Repo Map 与 textual tool descriptions 的顺序。

实际结果：

| Variant | Mean input | Mean cached input | Mean uncached input | Cache fraction | Mean latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| legacy_dynamic_first | 4514 | 0 | 4514 | 0.0 | 3.85s |
| stable_prefix | 4514 | 0 | 4514 | 0.0 | 3.10s |

两组 Provider-reported `cached_input_tokens` 都为 `0`，因此这次受控实验没有提供可用于定量比较 layout cache effect 的证据。

同时，之前真实 Coding Agent Trace 中已经观察到过非零 provider-reported `cached_tokens`，说明 Forge 当前 `OpenAICompatBackend` 的 cache usage 解析链至少在真实 Agent 调用中可以收到并记录非零值；不能把本次 microbenchmark 的 0 解读为“Provider 不支持缓存”或“Forge 无法解析缓存”。

`3.85s → 3.10s` 也不作为性能结论：样本只有 4 次 measured call / variant，且没有 cache hit 证据，Provider latency 波动无法与 prompt layout 因果绑定。

## Prompt Cache 最终决策

不再继续追加 Provider-specific cache 实验。

原因：

1. stable-prefix 的结构设计目标明确，生产代码和 deterministic layout regression 已经固定；
2. 当前 gateway 的受控 A/B 没有产生可比较的 cached-token 数据；
3. 真实 Agent Trace 已证明 provider cache telemetry 能出现非零值，但不足以把 cache 收益归因到 layout；
4. 继续追加 identical-control、更多重复或第二 Provider 的成本/收益不高，不属于 Repo Map 主线 blocker。

因此 RM4 最终状态定义为：

> **DONE（structural optimization，no quantitative provider-cache claim）**

可以说：

> 将稳定 system rules / tool descriptions 放在动态 Repo Map 前，尽量扩大支持 prefix caching 的 Provider 可复用前缀；该优化作为结构设计保留，但不宣称缓存命中率提升百分比。

不可以说：

- “Prompt Cache 命中率提升 X%”；
- “stable-prefix 实测降低输入 token X%”；
- “3.10s 证明 layout 比 3.85s 更快”；
- “当前 Provider 不支持缓存”。

## P2 最终状态

P2 到此收口，状态为 **DONE**。

最终可引用证据：

```text
Frozen retrieval (12 cases)
MRR:                  0.096954 → 0.318750
budget target recall: 0.364914 → 0.635251

Persistent / Incremental
12/12 strict semantic/ranking/visible/rendering equivalence
warm load / changed-file incremental update 避免不必要 full rebuild

Real-model small sample
4 cases × 4 variants × 1 run = 16 runs
all variants: verifier 4/4
Incremental Query-aware: completion 4/4, mean total tokens 59.5k, mean latency 64.7s
```

边界：真实模型结果是 single-run-per-cell small sample，不外推总体 Agent success rate；Prompt Cache 只保留结构优化，不形成定量效果主张。

## 验证入口

```bash
pytest -q tests/test_repo_map_prompt_cache_ablation.py tests/test_repo_map_trace_analysis.py tests/test_evidence_pack.py
python -m evals.verify_evidence_pack
python -m evals.verify_p2_repo_map_real
```

本部分结束，后续不再继续扩展 P2 Repo Map / Prompt Cache 实验，除非未来有新的明确需求。
