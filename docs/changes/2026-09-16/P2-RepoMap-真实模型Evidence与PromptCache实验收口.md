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

## Prompt Cache A/B

新增 `evals/repo_map_prompt_cache_ablation.py`。

这是 Real-provider Controlled Microbenchmark，不是 Agent benchmark。

唯一实验变量是 system prompt 中 dynamic Repo Map 与 textual tool descriptions 的顺序。其余保持一致，并用真实生产 ToolRegistry 的 12 个 tool schemas。

默认每个 layout：

```text
1 warmup + 4 measured calls
```

默认 `max_tokens=64`，降低输出成本。

采集：input tokens、provider-reported cached input tokens、uncached input tokens、cache write tokens、provider usage 是否 estimated、latency。

正式命令：

```bash
python -m evals.repo_map_prompt_cache_ablation \
  --config config/default.yaml \
  --repetitions 1 \
  --measured-calls 4 \
  --max-tokens 64 \
  --output ../forge-agent-evals/repo-map-prompt-cache-ab
```

## 新增验证

```bash
pytest -q tests/test_repo_map_prompt_cache_ablation.py tests/test_repo_map_trace_analysis.py tests/test_evidence_pack.py
python -m evals.verify_evidence_pack
python -m evals.verify_p2_repo_map_real
```

真实 Provider cache A/B 需要用户本地 API Key，不在离线测试中执行。

## Claim boundary

当前可以报告 16-run small-sample observed values，但不能外推总体 Agent success rate。

在 Prompt Cache A/B 实际执行前，仍不能写 cache improvement 百分比。
