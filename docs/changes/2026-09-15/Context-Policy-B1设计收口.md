# Context Policy B1 离线 Benchmark 设计收口

日期：2026-09-15
分支：`dev`
设计基线：`dev@2cae1f4add534d8fb90ae830ef659d6ddd1a3c93`

## 1. 目标

B1 只评估 Context Policy 本身，不运行真实 Coding Agent，不让模型自主修改仓库。

目标回答两个问题：

1. C4/C5 到底把 model-visible context 压缩了多少；
2. 压缩以后，早期硬约束、Action/Observation 配对、最新测试状态、Working Set、checkpoint lineage、repo revision 等关键状态是否仍然正确。

B1 不负责回答“最终任务成功率是否提升”。真实 Agent 成功率属于 B2。

## 2. 复用现有评测基础设施

仓库已有两套可复用范式：

- `evals/repo_map_ablation.py`：固定 fixture / 固定 revision，输出 `raw.jsonl + report.json + report.md`；
- `evals/harness.py` / `evals/run.py`：强调原始证据独立保存，verifier 不进入模型可见 History。

B1 沿用 `evals/` 目录和机器可读原始结果，不另建独立 benchmark 框架。

不修改现有 `evals/harness.py` / `evals/run.py`；Context Policy benchmark 使用独立 runner，避免把 coding-task EvalResult schema 强行扩展成 context schema。

## 3. Benchmark 两层模式

### 3.1 Replay 模式：默认、完全确定性

固定 history fixture 上运行三种 Context variant：

1. `budget_trim_only`
2. `deterministic_pruning`
3. `hybrid_compaction`

Hybrid 在 Replay 模式中使用 fixture semantic summarizer：semantic fields 由 fixture 明确给出，不调用真实 API。

该模式验证：

- pressure / token 计算；
- Stage A pruning；
- Stage B structured view；
- recent tail；
- Action/Observation unit；
- checkpoint / lineage；
- deterministic evidence；
- repo fingerprint；
- canonical history 不被修改。

Replay 模式适合单测和 CI，不产生模型成本，也不把 LLM 随机性混进 Context policy correctness。

### 3.2 Live Semantic 模式：只评估 C5 summary call

使用同一套固定 history fixture，但 `hybrid_compaction` 的 semantic summarizer 改为真实 `LLMSemanticSummarizer`。

仍然不运行 Agent、不执行 Tool、不让模型生成历史；唯一真实模型调用是 C5 的 `record_context_summary`。

该模式测：

- 中文 / 英文 / 混合语言 Hard Constraints 保留；
- semantic extraction 稳定性；
- summary input/output tokens；
- cached tokens（provider 返回时）；
- semantic duration；
- malformed/failure rate。

正式 Live Semantic 结果必须冻结：

- Forge commit；
- fixture hash；
- provider；
- protocol；
- model；
- threshold / target_ratio / keep_recent_tokens / budget；
- semantic packet 上限；
- repeat count。

API key 只从现有 config/environment 读取，永不写入结果。

## 4. Variant 定义

所有 variant 共享完全相同的：

- canonical history；
- `TokenBudget`；
- system text；
- tool schemas；
- repo-map diagnostic text；
- final TokenBudget safety trim。

### V1 `budget_trim_only`

不运行 Compaction policy。

链路：

```text
canonical history
  -> history_limit_for_request()
  -> trim_history()
  -> final model-visible view
```

这是 C1/C2 之前常见的“只靠最终 token trim”基线。

### V2 `deterministic_pruning`

当 raw full-request pressure 达到 threshold 后：

```text
canonical history
  -> protected recent tail
  -> DeterministicToolPruner
  -> 不进入 Stage B
  -> final TokenBudget trim
```

该 variant 是 B1 专用消融，不修改生产 `TraceableCompaction`。

### V3 `hybrid_compaction`

直接运行生产 `TraceableCompaction`：

```text
canonical history
  -> full request pressure
  -> Stage A pruning
  -> still high?
      -> Stage B semantic structured compaction
  -> final TokenBudget trim
```

Replay 模式注入 fixture summarizer；Live Semantic 模式注入真实 `LLMSemanticSummarizer`。

### Raw diagnostic

额外记录 canonical raw pressure/tokens，但 `raw` 不作为正式 variant，因为它可能超过窗口，不能代表真实可发送请求。

## 5. Final View 必须统一

Policy 输出之后，所有 variant 都再经过与 Agent 一致的最终 history limit：

```text
history_limit = TokenBudget.history_limit_for_request(system_text, tools)
final_view = TokenBudget.trim_history(policy_view, history_limit)
```

否则只比较 Compaction 输出而不经过 safety trim，会高估实际模型可见信息。

## 6. Fixture 形式

新增：

`evals/fixtures/context_policy_benchmark.json`

schema_version = 1。

每个 case 至少包含：

```text
id
current_goal
budget_tokens
threshold
target_ratio
keep_recent_tokens
max_summary_chars
history_spec
expected_semantic_fields
expected_invariants
applicable_variants
```

为了避免 JSON 放入数万字符重复文本，`history_spec` 允许 deterministic repeat/template 描述，但 fixture 展开后必须计算 `expanded_history_sha256` 并写入 raw result。

也就是说：fixture 可以紧凑，实际 benchmark 输入必须可哈希、可重放、可审计。

每条 Action/Observation fixture 都带稳定逻辑 id。物化时通过真实 `EventLog.log_action()` / `log_observation()` 获取 event_ref，再写入 `LLMMessage.event_ref`，因此可以验证 source event 是否真的存在于原始 EventLog，而不只是验证一个虚构字符串。

## 7. 统一压力参数

首版默认压力配置：

```text
budget_tokens = 4096
threshold = 0.80
target_ratio = 0.50
keep_recent_tokens = 640
max_summary_chars = 1600
```

这些是 stress-scale benchmark 参数，不宣称等价于某个 provider 的真实 80k/128k window。

Case 只有在为了触发特定生命周期条件时才允许 override，并必须把 override 写进 fixture 和 raw result。

`TokenBudget` 当前 reserve 为总预算 15%，B1 不自行重写该逻辑。

## 8. 七个固定 Case

### B1-1 `early-hard-constraint`

目的：验证很早出现、已经远离 recent tail 的硬约束，在压缩后是否仍然对模型可见。

历史结构：

```text
user: 当前目标
user: “不要修改 agent/core.py；必须保持 public API 稳定”
大量后续正常 Action/Observation / 用户补充
recent tail: 与上述约束无重复
```

期望 semantic facts：

```text
Hard Constraints:
- agent/core.py 不可修改
- public API 必须保持稳定
```

评分不用要求逐字复现，使用每条 constraint 的 required tokens：

```text
["agent/core.py"]
["public API"]
```

核心指标：

- `hard_constraint_recall`
- `constraint_visible_after_final_trim`
- `canonical_unchanged`

Hybrid Live 的硬门禁：每条约束都必须在 final model-visible view 中可识别。

### B1-2 `huge-tool-output`

目的：验证 C4 Stage A 对旧大 Tool 输出的实际 token 收益，同时保证 recent raw context 不被破坏。

历史包含：

- 旧 `file_read SUCCESS`，正文含唯一 `OLD_FILE_BODY_MARKER`；
- 旧 `shell SUCCESS` 大输出；
- 旧 `test SUCCESS` 大日志；
- 每条都有真实 event_ref；
- recent `file_view` / user message 带 `RECENT_RAW_MARKER`。

Invariant：

- pruning/hybrid final view 不出现 `OLD_FILE_BODY_MARKER`；
- recent `RECENT_RAW_MARKER` 原样保留；
- canonical history 中旧原文仍存在；
- EventLog 中对应 Observation 仍可按 event_ref 找到；
- Stage A checkpoint 的 `pruned_event_ids` 正确；
- token 数必须下降，但首版不预设“下降 X%”这种 claim 阈值。

### B1-3 `action-observation-pair`

目的：验证所有 Context 路径都不产生 orphan Action / Observation。

构造：

- 多组 assistant Action + user Observation；
- 大小交错；
- 预算故意卡在中间 pair 附近；
- recent tail 也包含一个完整 pair。

Invariant：

- `orphan_actions = 0`
- `orphan_observations = 0`
- recent pair 两条都保留或两条都不保留，不能拆开；
- final trim 后仍满足。

该 case 三个 variant 都必须通过。

### B1-4 `superseded-state`

目的：验证 Structured State 不把已经被后续结果覆盖的旧失败写成当前 unresolved failure。

历史：

```text
old: pytest ERROR -> OLD_TEST_FAILURE_MARKER
old: file/tool ERROR
later: 同类测试 pytest SUCCESS -> LATEST_TEST_PASS_MARKER
recent: 成功验证状态
```

Invariant（Hybrid）：

- `Verification State = PASS`；
- final view 可定位 `LATEST_TEST_PASS_MARKER` 或等价 latest-pass evidence；
- `Unresolved Failures` 不包含已被 later pass supersede 的旧 pytest failure；
- `OLD_TEST_FAILURE_MARKER` 不得被表述成当前 unresolved state；
- deterministic evidence 的 source event_ref 指向最新 test observation。

### B1-5 `repeated-compaction`

目的：验证 active view、re-compaction 和 checkpoint lineage。

执行序列：

1. 长 history 第一次达到 threshold，触发 Stage B；
2. 不增加 history，再调用一次 policy；
3. 追加同一 Task 的 raw delta，但仍低于 active-view threshold；
4. 再追加足够 delta，使 active view 再次达到 threshold，触发第二次 Stage B。

Invariant：

- 第一次 summary call count = 1；
- 第 2/3 步不会新增 semantic call；
- 第 4 步 call count = 2；
- checkpoint 2 的 `previous_checkpoint_id == checkpoint 1 id`；
- 第二次 semantic input 从 canonical history 构造，不包含第一次 `[Compacted earlier context ...]`；
- canonical history hash 不因 compaction 改变。

### B1-6 `resume-long-session`

目的：验证 C3 round-boundary preflight / resume lineage 的核心语义。

构造：

- 长 canonical history；
- 恢复一个历史 checkpoint cursor，例如 `checkpoint-before-resume`；
- 追加当前 round user input；
- 以 `step=1` 调用 Context policy，模拟 resume/new-round preflight。

Invariant：

- Fresh active summary 不从持久化状态恢复；
- 若压力足够，step 1 即可形成新的 compaction view；
- 新 checkpoint `previous_checkpoint_id == checkpoint-before-resume`；
- 当前 user input 在 final view 中恰好出现一次；
- Goal 使用当前 `task.description`；
- canonical history 不被重写。

### B1-7 `dirty-repo-revision`

目的：验证 checkpoint repo revision 真正覆盖 HEAD + working tree，而不是只看 HEAD。

构造：

1. 临时 git repo 建立 baseline commit；
2. 触发一次 checkpoint，记录 fingerprint A；
3. 修改 tracked file / 增加 untracked file，但不 commit；
4. HEAD 保持不变，再触发新的 checkpoint，记录 fingerprint B。

Invariant：

- HEAD A == HEAD B；
- fingerprint A != fingerprint B；
- checkpoint.repo_revision 与调用时 `repository_fingerprint()` 完全相等；
- 不需要 semantic model 即可验证此 case。

## 9. 指标

### 9.1 Token / Pressure

每行 raw result 记录：

```text
canonical_history_tokens
policy_view_tokens
final_view_tokens
compaction_ratio
projected_input_before
projected_input_after
pressure_before
pressure_after
available_input_tokens
final_within_available
```

其中：

```text
compaction_ratio = 1 - final_view_tokens / canonical_history_tokens
```

Token 只描述当前 estimator 下的结果；metadata 必须记录 `tiktoken_available`。

### 9.2 Preservation

```text
hard_constraint_recall
recent_raw_recall
orphan_actions
orphan_observations
latest_test_status_correct
stale_failure_violations
working_set_recall
working_set_precision
source_event_coverage
raw_event_traceable
canonical_unchanged
```

`source_event_coverage` 只统计应被 checkpoint/source refs 覆盖的 old region event_refs，不把 pruning-only 和 Stage-B 的字段语义混为一谈。

### 9.3 Lifecycle

```text
checkpoint_count
checkpoint_atomic
lineage_correct
active_view_reused
summary_call_count
summary_input_tokens
summary_output_tokens
summary_cached_tokens
semantic_duration_ms
semantic_failure
packet_truncated
repo_revision_correct
```

## 10. Checkpoint Atomic 判定

Stage B checkpoint 必须同时满足：

- 有 exactly one `CONTEXT_COMPACTED` 对应当前 checkpoint；
- `CompactionEntry.checkpoint_id == CompactionCheckpoint.checkpoint_id`；
- `summary_hash` 一致；
- `source_event_ids` 与 canonical dropped region 对齐；
- semantic failure 若走 structured fallback，也只能形成一个完整 checkpoint，不能出现半 entry / 半 checkpoint。

Pruning-only checkpoint 没有 `CompactionEntry` 是设计内行为，不算 atomic failure。

## 11. Live Semantic 评分

Live Semantic 只跑需要自然语言理解的 case：

- `early-hard-constraint`
- `superseded-state`（semantic progress 辅助，test truth 仍 deterministic）
- `repeated-compaction`
- `resume-long-session`

其中 deterministic truth 不让 LLM 决定。

首版建议每 case 3 repeats。

聚合时必须先“同一 case 多次 repeat 求均值/通过率”，再跨 case 求 variant 汇总，不能让 3 repeats 的 Hybrid 在总体统计里获得三倍权重。

Live Semantic 报告必须单独列：

```text
semantic_pass_rate
constraint_recall
summary_calls
summary_input_tokens
summary_output_tokens
semantic_duration_ms p50/p95
```

## 12. Pass / Fail 门禁

B1 不是通过一个平均分就算成功，而是结构 invariant hard gate。

必须全部满足：

1. 所有 variant：`orphan_actions == 0` 且 `orphan_observations == 0`；
2. pruning/hybrid：recent raw required marker 100% 保留；
3. hybrid：`early-hard-constraint` hard constraint recall = 1.0；
4. hybrid：`superseded-state` latest test status 正确且无 stale failure；
5. hybrid：`repeated-compaction` active reuse + lineage + no-summary-of-summary 全通过；
6. hybrid resume：current user exactly once、lineage 正确；
7. dirty repo：same HEAD 下 working-tree 变化必须改变 repo fingerprint；
8. 所有 Context policy variant：canonical history hash 不变；
9. 最终 view 在 fixture 正常配置下必须 `final_within_available=true`。

Token reduction 不设人为“至少 30%/50%”门槛，先报告实际数据，避免为了好看调 fixture。

## 13. 输出目录

默认：

`evals/results/context_policy_benchmark/`

产物：

```text
raw.jsonl
report.json
report.md
metadata.json
```

`raw.jsonl` 每条为一个 case × variant × repeat 的原始证据。

`metadata.json` 至少记录：

```text
schema_version
runner_revision
fixture_sha256
expanded_fixture_sha256
timestamp
python_version
tiktoken_available
semantic_mode
provider/protocol/model（live 时）
threshold
target_ratio
budget defaults
```

不得记录 API key。

## 14. CLI 设计

计划命令：

```bash
python -m evals.context_policy_benchmark \
  --semantic-mode fixture \
  --output evals/results/context_policy_benchmark
```

真实 semantic spot-check：

```bash
python -m evals.context_policy_benchmark \
  --semantic-mode live \
  --config config/default.yaml \
  --repetitions 3 \
  --output evals/results/context_policy_benchmark-live
```

允许 `--provider / --protocol / --model` 覆盖，但必须写入 metadata。

## 15. B1 实施文件范围

建议严格控制为 3 个代码/fixture 文件：

1. 新增 `evals/context_policy_benchmark.py`
   - fixture loader/materializer；
   - 三 variant runner；
   - metrics / invariant verifier；
   - raw/report writer；
   - fixture/live semantic adapter。

2. 新增 `evals/fixtures/context_policy_benchmark.json`
   - 七个 case；
   - 固定 stress budget；
   - semantic expected facts；
   - case-local invariant。

3. 新增 `tests/test_context_policy_benchmark.py`
   - fixture schema；
   - 三 variant final-view pipeline；
   - 7 case deterministic invariants；
   - report aggregation；
   - live mode 只测试 wiring，用 MockBackend，不调用网络。

文档同步另计：

- `../../plans/archive/CONTEXT_COMPACTION_TODO.md`
- B1 完成证据最终保存在 `../../../evals/results/context_policy_benchmark/report.json`，未另建原计划中的独立改动日志。

## 16. 明确不改

B1 不应修改任何生产 Context / Agent 代码：

```text
context/compaction.py
context/structured_compaction.py
context/token_budget.py
context/tool_pruning.py
agent/core.py
agent/runner.py
entry/chat.py
agent/session.py
config/default.yaml
provider backend
```

如果 benchmark 发现 bug，先把失败证据写进 raw result / TODO，再单独提出生产修复范围；不能为了让 benchmark 通过直接改生产策略。

## 17. 与 B2 / C6 的关系

B1 只证明 Context policy 的局部性质和 token 成本。

B1 不能直接宣称：

- Agent 任务成功率提升；
- solved-task token 成本下降；
- Agent 能自主恢复所有旧 Tool 原文。

这些分别属于 B2 和可选 C6。

只有 B1/B2 出现稳定“模型需要被压缩掉的旧 Tool 原文才能继续”的失败样本，才启动 `context_recall(event_ref)`。

## 18. Claim 门禁

B1 正式跑完之前，只能说：

> 已实现 Hybrid Structured Compaction，并完成单元/回归测试。

B1 跑完后，可以基于固定 fixture 报告：

- Context token reduction；
- hard constraint preservation；
- stale-state correctness；
- lifecycle/checkpoint correctness；
- semantic summary call cost。

但“真实 Coding Agent 成功率”和“每 solved task token”仍必须等 B2。
