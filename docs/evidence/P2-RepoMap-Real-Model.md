# P2 Repo Map — Real-model Small Sample Evidence

日期：2026-09-16

本文件是 `docs/evidence/README.md` 的 P2 real-model 补充证据。原
`evals/results/repo_map_agent_ablation/` 保留 provider-free harness 的历史
`not_executed` artifact；真实运行单独冻结到：

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
4. Provider 返回了非零 `cached_input_tokens`；这证明真实 cache usage telemetry 可被 Forge 读取，不等于当前 prompt layout 已经证明 cache 改善。

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

## Prompt Cache 下一步

新增受控真实 Provider A/B：

```text
evals/repo_map_prompt_cache_ablation.py
```

它不运行完整 Coding Agent，只保持以下因素一致：

- model/provider；
- 真实 12 个 tool schemas；
- user message；
- dynamic repository payload 的形状与长度；
- warmup / measured call 数；

唯一变化：

```text
legacy_dynamic_first:
stable rules → dynamic Repo Map → textual tool descriptions

stable_prefix:
stable rules → textual tool descriptions → dynamic Repo Map
```

正式执行前不写 cache improvement 数字。

## Claim boundary

现在可以说：

> 在 DeepSeek-v4.1-flash 的 4-case × 4-variant 单次 real-model small sample 中，四组 patch 均 4/4 通过 hidden verifier；Incremental Query-aware 观察到 4/4 completion，mean total tokens 约 59.5k、mean latency 约 64.7s。

必须同时说明这是 `4 cases × 1 run/cell` 的观察值。

不能说：

- “Repo Map 已证明总体成功率提升 X%”；
- “Incremental 稳定优于所有方案”；
- “Static Repo Map 代码成功率为 0%”；
- “Prompt Cache 命中率已经因为 layout 提升 X%”；
- “59.5k / 64.7s 是跨模型、跨仓库稳定结果”。
