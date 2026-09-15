# Context Compaction C4：Deterministic Tool-output Pruning 设计收口

日期：2026-09-15

## 0. C3 前置缺口

进入 C4 前复核真实 CLI 入口发现：`entry/cli.py` 当前创建 `ChatSession` 时没有传入 `prepare_next_turn`，因此普通 `agent chat` 还没有真正启用 C1~C3 已实现并通过测试的 `TraceableCompaction` policy。

C4 实施时必须先补这一条 production wiring，并增加 CLI/Chat 回归，避免后续 pruning 只存在于测试注入场景。

## 1. C4 目标

C4 只做 Stage A deterministic pruning：

```text
canonical history
      |
      v
full request pressure
      |
 pressure < threshold ------> no-op
      |
      v
old Tool-output pruning
      |
      v
recompute pressure
   |                 |
below threshold      still high
   |                 |
pruned model view    current extractive compaction
                     (C5 后续再替换为 structured compaction)
```

核心原则：

1. canonical `ConversationHistory` 永远不改；
2. 只改 model-visible copy；
3. recent raw tail 不 prune；
4. Action/Observation unit 不拆；
5. 用户消息、Reflection、Hard Constraint 不 prune；
6. C4 v1 只 prune **SUCCESS** Observation，ERROR/TIMEOUT 原样保留；
7. 所有规则 deterministic，不调用 LLM；
8. EventLog 中的原始 Observation 继续作为审计源。

注意：marker 中的 `full_output_available=true` 语义是“该次进入 canonical Observation 的完整 output 仍可从 EventLog 审计回查”；底层 Tool 自身可能已经做过安全截断，例如 shell 8k chars、pytest 6k chars，因此不声称 EventLog 保存了进程产生的无限原始 stdout。

## 2. 当前 History 可利用的稳定结构

`agent/core.py::_format_action_for_history()` 当前写入：

```text
Thought: ...
Action: <tool>
Params: {...}
```

`agent/core.py::_format_observation_for_history()` 当前写入：

```text
[Tool: <tool> | SUCCESS/ERROR]
<output>
Error: ...
```

同时 `LLMMessage.event_ref` 保存 EventLog 引用。

因此 C4 不需要修改 `Observation`、`ToolResult`、具体 Tool 或 LLM provider；Pruner 可以只识别这套内部稳定格式，并保留原 message role/event_ref。

## 3. Recent tail 保护

C4 复用 C1 的 `HistoryUnit` / `recent_history_units()`：

1. 用 **原始 canonical history** 计算 `keep_recent_tokens` 对应的 recent tail；
2. recent tail 内消息完全保持原文；
3. 只处理 tail 之前的完整 HistoryUnit；
4. 不因为 pruning 后 token 变小而向后重新定义 recent raw tail。

这样保证“最近 N token 原始上下文”的语义不会因 Stage A 自己改变。

## 4. V1 Pruning 规则

### 4.1 `file_read` SUCCESS

仅当 Observation output 超过最小 token 阈值时处理。

保留：

- `[Tool: file_read | SUCCESS]`；
- `File: <path> (<N> lines total)` 首行元信息（若存在）；
- `event_ref`；
- `full_output_available=true`；
- 原 output token/char 规模诊断。

删除旧文件正文行。

不处理 `file_view`：窗口读取通常是 Agent 主动挑选的局部工作集，第一版先保守保留。

### 4.2 `shell` SUCCESS

仅处理超过最小 token 阈值的旧输出。

保留：

- Tool/status；
- paired Action 中原命令和 params 保持不变；
- stdout 的 deterministic head/tail preview；
- `event_ref`；
- `full_output_available=true`；
- 原 output 规模。

不处理 shell ERROR；失败输出可能仍是根因证据。

### 4.3 `test` SUCCESS

当前 `PytestTool` 成功时通常已经只留下最终统计行，因此绝大多数不会达到 prune 阈值。

若未来/fixture 中成功 output 仍很大：只保留最终非空统计/尾部摘要 + marker。

`test` ERROR 第一版完全不 prune。当前 `PytestTool` 失败结果已经做过 6k 上限与 failure summary 提取，继续保留比二次启发式裁剪更安全。

### 4.4 重复 search/find SUCCESS

第一版仅处理 **exact duplicate interaction**：

```text
same assistant Action content
+ same Observation content
+ same tool/status
```

工具范围：

- `search_text`
- `find_files`
- `find_symbol`

从新到旧扫描，最新一份保留原文；更旧的完全重复结果替换成：

```text
[Pruned duplicate tool output]
duplicate_of_event_ref=<newer-event-ref>
full_output_available=true
```

不做“语义相似”去重，避免错误合并不同搜索结果。

## 5. 明确不做的规则

C4 v1 不做：

- 失败 Observation pruning；
- superseded error/test 状态判断；
- `file_view` pruning；
- `git diff/status` 特殊 pruning；
- 根据代码语义判断 Tool 输出重要性；
- LLM summary；
- EventLog recall tool。

“已被后续状态覆盖的 Observation”只有等 C5 structured state 或独立可证明规则出现后再处理。

## 6. 模块设计

### 新增 `context/tool_pruning.py`

建议结构：

```text
DeterministicToolPruner
PruningResult
ParsedObservation / internal parser helpers
```

职责：

- 输入 canonical message copy + protected recent boundary；
- 按完整 HistoryUnit 识别可处理的 Action/Observation；
- 只替换 model-view Observation content；
- 保留 role、tool_call_id、event_ref；
- 返回 pruning stats。

建议 `PruningResult` 至少记录：

```text
messages
pruned_event_ids
before_tokens
after_tokens
pruned_units
```

### `context/compaction.py`

`TraceableCompaction` 在 full-request pressure 高时：

1. 先调用 `DeterministicToolPruner`；
2. 重算 request pressure；
3. 如果 Stage A 已降到 threshold 以下，直接返回 pruning-only `history_override`；
4. 如果仍高，现阶段继续运行已有 `extractive-v1`，但 summary 输入使用已经 pruned 的旧 Tool 输出，而不是重新吞原始大输出；
5. canonical history 始终不修改。

为 benchmark/Trace 增加 checkpoint 证据字段：

```text
pruning_method
pruned_event_ids
pruned_before_tokens
pruned_after_tokens
```

Pruning-only 也形成审计 checkpoint；但不创建伪 `CompactionEntry.summary_text`。`CompactionEntry` 仍只用于真正 summary/compaction state。

checkpoint lineage 统一以 `_lineage_checkpoint_id` 为准，使 pruning-only checkpoint 也能参与前后继审计。

继续复用现有 `EventType.CONTEXT_COMPACTED`，通过 `pruning_method/summary_method` 区分 Stage A 与 Stage B；C4 不为此新增 EventType。

## 7. Production wiring

`entry/cli.py` 创建真实 `ChatSession` 时实例化 **一个会话级** `TraceableCompaction`：

```text
TraceableCompaction instance
      |
      +-- Round 1
      +-- Round 2
      +-- resume lineage
      +-- later C4 pruning
```

必须是同一 ChatSession 生命周期内复用一个实例，不能每轮 new，否则 C3 checkpoint lineage 会丢。

当前不新增 `config/default.yaml` 配置；threshold/keep_recent_tokens 继续使用 `TraceableCompaction` 当前默认值。正式 benchmark 会通过显式构造参数冻结策略。

## 8. C4 最小修改范围

生产代码：

1. `entry/cli.py`
   - 补 C3 production wiring；
   - 将同一 `TraceableCompaction` 实例传入 `ChatSession(prepare_next_turn=...)`。

2. 新增 `context/tool_pruning.py`
   - Stage A deterministic pruning 实现。

3. `context/compaction.py`
   - 接入 Stage A；
   - pruning-only override；
   - intermediate pressure；
   - pruning checkpoint evidence；
   - lineage 对 pruning-only checkpoint 兼容。

测试：

4. 新增 `tests/test_tool_pruning.py`
   - 单元规则测试。

5. `tests/test_compaction.py`
   - Stage A + pressure + Stage B 集成测试。

6. `tests/test_chat.py`
   - 真实 CLI 创建 ChatSession 时确实注入 Context policy；
   - 防止 C3 production wiring 再次漏掉。

明确不改：

- `agent/core.py`
- `agent/task.py`
- `agent/session.py`
- `context/token_budget.py`
- `tools/*`
- `config/default.yaml`
- provider

## 9. 测试矩阵

### Tool-pruner unit tests

1. old large `file_read` SUCCESS 被缩成 marker + File metadata；
2. recent large `file_read` 完全保留；
3. user/hard constraint 完全不改；
4. ERROR Observation 完全不改；
5. large shell SUCCESS 保留 head/tail + marker；
6. small shell output 不处理；
7. large test SUCCESS 只留尾部摘要；
8. failed test 不处理；
9. exact duplicate search：旧结果 prune、最新结果保留；
10. 不同 query/output 不误去重；
11. Action/Observation unit 数量与配对不变；
12. 相同输入两次执行得到相同 model-view/pruned_event_ids。

### Compaction integration tests

1. pressure 高 -> pruning 后低于 threshold：只产生 pruning view，不出现 extractive summary marker；
2. pressure 高 -> pruning 后仍高：Stage B summary 输入中不再包含完整旧 Tool body；
3. canonical history 不变；
4. event_ref/source coverage 保留；
5. checkpoint 记录 pruning before/after token evidence；
6. recent raw tail 不受 Stage A 影响；
7. C1~C3 原有 tests 继续通过。

### CLI/Chat regression

- `entry.cli.chat` 创建 `ChatSession` 时 `prepare_next_turn` 非 None 且为会话级单实例；
- Fresh Round 1 仍由 C3 Runner 条件跳过 preflight；
- Round 2+ 才按 pressure 实际运行 policy。

## 10. 验收口径

C4 完成只允许声称：

- Forge 支持 deterministic old Tool-output pruning；
- canonical history 不被 pruning 修改；
- recent raw tail 保留；
- 原 Observation 仍可通过 EventLog 审计回查；
- pruning 前后 token estimate/checkpoint 有原始证据。

在 B1/B2 正式 benchmark 前仍不能声称具体 Token 降幅、成功率提升或“不丢信息”。
