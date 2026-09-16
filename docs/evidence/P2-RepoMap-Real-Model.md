# P2 Repo Map — Real-model Small Sample Evidence

日期：2026-09-16

本文件是 `docs/evidence/README.md` 的 P2 real-model 补充证据。原 `evals/results/repo_map_agent_ablation/` 保留 provider-free harness 的历史 `not_executed` artifact；真实运行单独冻结到：

```text
evals/results/repo_map_agent_ablation_real_v1/
```

这样避免覆盖历史结果，同时保留真实运行的独立证据层。

## 冻结协议

- runner revision：`af7160a15e508e379db74ed7c7e84316ee86b906`
- provider：`openai`（OpenAI-compatible）
- model：`deepseek-v4.1-flash`
- cases：4
- variants：4
- repetitions：1
- real-model runs：16
- max steps：12
- budget tokens：40000
- production path：`ExecutionRunner → Agent → Tool lifecycle → Completion Guard → Independent Acceptance → Trace v2`

Variants：

```text
A. no_repo_map
B. static_repo_map
C. query_aware_repo_map
D. incremental_query_aware_repo_map
```

## 观察结果

| Variant | Runs | Agent solved | Hidden verifier pass | Mean total tokens | Mean cached input | Mean latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| no_repo_map | 4 | 3 | 4 | 65,055.25 | 26,048 | 70.45s |
| static_repo_map | 4 | 0 | 4 | 96,704.25 | 39,776 | 96.97s |
| query_aware_repo_map | 4 | 3 | 4 | 67,349.25 | 26,752 | 70.40s |
| incremental_query_aware_repo_map | 4 | 4 | 4 | 59,541.00 | 21,728 | 64.71s |

机器校验：

```bash
python -m evals.verify_p2_repo_map_real
```

关键事实：

1. 四组共 16 个 patch 全部通过独立 hidden verifier。
2. `static_repo_map` 的 `0/4 solved` 不是“代码没改对”；4/4 verifier 都通过，但 Agent 在 12-step 资源边界前没有完成 completion protocol，最终为 `resource_exhausted`。
3. 本次单次小样本中，Incremental Query-aware 是唯一 `4/4 solved + 4/4 verifier` 的组，同时观察到最低 mean total tokens 与最低 mean latency。
4. Provider 在真实 Agent Trace 中返回过非零 `cached_input_tokens`，说明 cache usage telemetry 可以被 Forge 读取；这本身不证明 prompt layout 带来 cache 改善。

## Exploration metric 修正

历史 `raw.jsonl` 中的：

```text
first_target_read_step
files_read
```

只统计 `file_read/file_view`。真实模型大量通过 `shell` 的 `cat/find/grep/python` 访问仓库，因此这些字段不能作为正式“定位效率”证据。

不重写历史 raw；新增：

```text
evals/repo_map_trace_analysis.py
```

用于从冻结 Trace v2 重算：

- `legacy_first_target_read_step`
- `first_target_access_step`
- `file_tool_files_read`
- `shell_referenced_files`
- `explicit_files_accessed`

其中 shell 只统计命令文本中可明确识别到的仓库文件路径，仍不把模糊递归搜索包装成精确 read count。

## Prompt Cache 结构与真实 A/B

生产 `agent/prompt.py` 保留：

```text
stable system rules
→ stable textual tool descriptions / tool schemas
→ dynamic Repo Map
→ conversation
```

设计动机是把动态仓库上下文尽量后移，为按前缀匹配缓存的 Provider 提供更长的稳定 prefix。

为了验证是否能形成定量 provider-cache claim，新增并执行：

```text
evals/repo_map_prompt_cache_ablation.py
```

受控变量：

```text
legacy_dynamic_first:
stable rules → dynamic Repo Map → textual tool descriptions

stable_prefix:
stable rules → textual tool descriptions → dynamic Repo Map
```

保持 model/provider、真实 12 个 tool schemas、user message、dynamic repository payload 形状、调用次数一致。

本次运行：

```text
repetitions = 1
warmup = 1 / variant
measured calls = 4 / variant
max output tokens = 64
```

结果：

| Variant | Mean input | Mean cached input | Mean uncached input | Cache fraction | Mean latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| legacy_dynamic_first | 4514 | 0 | 4514 | 0.0 | 3.85s |
| stable_prefix | 4514 | 0 | 4514 | 0.0 | 3.10s |

两组 provider-reported cached input 都为 0，因此这次实验不能支持“stable-prefix 提升 cache hit / 降低 uncached input”的定量结论。

同时真实 Agent Trace 已出现非零 cached tokens，所以不能把该 0 解读为“Provider 不支持缓存”或“Forge 没解析 cache usage”。

Latency `3.85s → 3.10s` 也不作为效果证据：样本小，且没有 cache-hit 差异，无法归因到 prompt layout。

最终决策：保留 stable-prefix 作为结构优化，不再追加 Provider-specific cache 实验，不形成 cache improvement 百分比主张。

## Claim boundary

现在可以说：

> 在 DeepSeek-v4.1-flash 的 4-case × 4-variant 单次 real-model small sample 中，四组 patch 均 4/4 通过 hidden verifier；Incremental Query-aware 观察到 4/4 completion，mean total tokens 约 59.5k、mean latency 约 64.7s。

必须同时说明这是 `4 cases × 1 run/cell` 的观察值。

Prompt Cache 可以说：

> 将稳定 rules/tool descriptions 放在动态 Repo Map 前，尽量扩大支持 prefix caching Provider 的可复用前缀；该优化按结构原则保留，但没有定量 cache improvement claim。

不能说：

- “Repo Map 已证明总体成功率提升 X%”；
- “Incremental 稳定优于所有方案”；
- “Static Repo Map 代码成功率为 0%”；
- “Prompt Cache 命中率已经因为 layout 提升 X%”；
- “stable-prefix 实测降低输入 token X%”；
- “3.10s 证明 layout 比 3.85s 更快”；
- “当前 Provider 不支持缓存”；
- “59.5k / 64.7s 是跨模型、跨仓库稳定结果”。

P2 Repo Map 到此收口，后续没有剩余 blocker。
