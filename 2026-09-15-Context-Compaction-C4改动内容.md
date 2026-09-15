# Context Compaction C4 改动内容

日期：2026-09-15

## 本轮目标

C4 实现 deterministic old Tool-output pruning，并补齐 C3 在真实 `agent chat` CLI 中漏掉的 Context policy production wiring。

处理顺序：

```text
canonical history
-> full request pressure
-> Stage A deterministic pruning
-> recompute pressure
   -> pressure below threshold: pruning-only model view
   -> still high: extractive-v1 fallback on the pruned old history
```

C4 不做 structured semantic summary、context recall、provider-native compact，也不修改 canonical `ConversationHistory`。

## 实际修改范围

确认范围允许 6 个代码/测试文件；实际只修改 5 个：

- 新增 `context/tool_pruning.py`
- `context/compaction.py`
- `entry/cli.py`
- 新增 `tests/test_tool_pruning.py`
- `tests/test_chat.py`

没有修改：

- `tests/test_compaction.py`：Stage A / Stage B 集成测试已集中放入新增的 `tests/test_tool_pruning.py`，避免重复 fixture；
- `agent/core.py`
- `agent/task.py`
- `agent/session.py`
- `context/token_budget.py`
- `tools/*`
- `config/default.yaml`

## 1. DeterministicToolPruner

新增 `context/tool_pruning.py`：

- 只操作 model-visible message copy；
- 用 Forge 内部稳定 Observation 头解析 Tool/status：`[Tool: <name> | SUCCESS/ERROR]`；
- 只 prune SUCCESS Observation；ERROR 原样保留；
- 只处理 protected recent tail 之前的消息；
- 保留 message role、`tool_call_id` 与 `event_ref`；
- 相同输入执行结果 deterministic；
- 返回 `PruningResult(messages, pruned_event_ids, before_tokens, after_tokens, pruned_units)`。

V1 规则：

1. `file_read` SUCCESS：旧大输出保留 `File: ...` 元信息与审计 marker，正文移除；
2. `shell` SUCCESS：旧大输出保留 deterministic head/tail preview；
3. `test` / `pytest` SUCCESS：旧大输出只保留尾部非空摘要；
4. `search_text` / `find_files` / `find_symbol`：只对 exact duplicate interaction 去重，最新一份保留，旧副本指向 `duplicate_of_event_ref`；
5. `file_view` 不处理；失败 Observation 不处理。

marker 中 `full_output_available=true` 只表示本次进入 canonical Observation 的完整 output 可从 EventLog 审计回查；不声称底层 Tool 在自身安全截断前产生的无限原始 stdout 被保存。

## 2. Recent raw tail 保护

`TraceableCompaction` 仍用原始 canonical history 调用 `recent_history_units()`，先固定 `tail_start`。

Stage A 只能修改 `tail_start` 之前的旧 Observation；pruning 后不会重新计算 recent boundary。因此 C1 的“最近 N token 原始上下文”语义保持不变。

## 3. Pruning-only checkpoint

当原 request pressure 已超过 threshold，但 Stage A 后降回 threshold 以下：

- 直接返回 pruning-only `history_override`；
- canonical history 不变；
- 不创建 `CompactionEntry`；
- checkpoint 使用 `summary_method="none"`；
- 记录 `pruning_method`、`pruned_event_ids`、`pruned_before_tokens`、`pruned_after_tokens`、`pruned_units`；
- pruning-only checkpoint 也更新 `_lineage_checkpoint_id`。

这样“只裁 Tool 输出”不会被伪装成 summary state，同时仍有完整前后继审计链。

## 4. Stage B fallback

若 Stage A 后完整 request pressure 仍高：

- 继续使用当前 `extractive-v1`；
- summary 输入改为 **已经 pruning 的旧 Tool view**；
- 不再把被 Stage A 判定为可裁的大块旧 Tool 正文重新塞进 summary；
- `source_event_ids` 仍从原 canonical dropped region 收集；
- checkpoint 同时记录 Stage A 和 Stage B 证据。

`previous_checkpoint_id` 统一直接取 `_lineage_checkpoint_id`，因此前一 checkpoint 即使是 pruning-only，下一 checkpoint 也能正确连接。

## 5. C3 production wiring 补齐

`entry/cli.py::chat()` 现在创建一个会话级：

```python
context_policy = TraceableCompaction()
```

并传入：

```python
ChatSession(..., prepare_next_turn=context_policy)
```

同一实例贯穿该 ChatSession 生命周期，因此 Round 2+、resume lineage 和 C4 pruning 都使用同一个 policy runtime state。

普通 `agent run` 没有因此默认启用 Chat Context policy。

## 6. 测试覆盖

新增 `tests/test_tool_pruning.py` 覆盖：

- old large `file_read` prune；
- recent `file_read` 保持原样；
- hard constraint/user message 不变；
- ERROR Observation 不 prune；
- large shell head/tail；
- small shell 不处理；
- large test SUCCESS 尾部保留；
- failed test 不处理；
- exact duplicate search 只 prune 旧副本；
- 不同 search 不误去重；
- deterministic 输出和 Action/Observation 数量保持；
- Stage A 足够时 pruning-only，不产生 extractive summary marker；
- Stage A 不足时 Stage B summary 不再包含旧 `file_read` 完整正文；
- canonical history 保持原样；
- checkpoint pruning token evidence。

`tests/test_chat.py` 在真实 CLI sandbox fixture 上新增断言：传给 `ChatSession` 的 `prepare_next_turn` 必须是 `TraceableCompaction`，防止 production wiring 再次遗漏。

## 当前提交

C4 设计基线：

`56fe9fb52a379a310e1be088c45426fca0c780e3`

C4 代码与测试完成点（创建本文档前）：

`a45751f76462ddde88b381d59644fe0f468e74b7`

## 验证状态

当前 ChatGPT 执行环境无法运行用户 WSL venv，因此不能声称 pytest 已通过。

用户 pull 后先运行：

```bash
python -m pytest -q \
  tests/test_tool_pruning.py \
  tests/test_compaction.py
```

再跑受真实 Chat wiring / Session lifecycle 影响的回归：

```bash
python -m pytest -q \
  tests/test_chat.py \
  tests/test_session_store.py \
  tests/test_day2.py
```

只有两组都真实通过后，C4 才标记完成。

## 已知边界

1. V1 不 prune ERROR/TIMEOUT、`file_view`、git diff/status，也不判断 superseded failure state；
2. Stage B 仍是 `extractive-v1`，structured state 属于 C5；
3. `event_ref` 仍是 Trace 审计引用，不是 Agent 自主 recall；
4. `entry/cli.py` 与 `tests/test_chat.py` 原文件使用 CRLF，本轮通过 GitHub Contents API 重写后行尾归一化为 LF，因此 compare 显示的行数远大于实际功能改动；
5. benchmark 前不得宣称具体 Token 降幅或成功率提升。
