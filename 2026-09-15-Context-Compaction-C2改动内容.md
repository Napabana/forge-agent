# Context Compaction C2 改动内容

日期：2026-09-15

## 本轮目标

在 C1 已完成 canonical history / HistoryUnit / token-based recent tail 的基础上，继续收口两个核心问题：

1. Compaction 触发从“只看 History 子预算”改为“看下一次完整模型请求压力”；
2. Compaction summary 不再永久写回 `ConversationHistory`，而是成为独立 `CompactionEntry`，只通过一次性的 model-visible history override 进入下一次 LLM 调用。

本轮不做：structured semantic summary、deterministic Tool-output pruning、RepositoryState working-tree hash、Session resume preflight、context recall、正式 benchmark。

## 实际修改范围

用户确认后只修改 4 个文件：

- `context/token_budget.py`
- `context/compaction.py`
- `agent/core.py`
- `tests/test_compaction.py`

未修改 `entry/chat.py`、`agent/session.py`、provider、evals、`config/default.yaml`。

## 1. `context/token_budget.py`

新增 `ContextPressure`，统一记录：total budget、reserve、available input、system tokens、tool schema tokens、repo map tokens（诊断字段）、history tokens、projected input、pressure ratio。

新增 `estimate_tool_schemas_tokens()`，使用 Forge 自己的 `LLMToolSchema` 字段序列化估算，不绑定具体 provider SDK。

新增 `TokenBudget.request_pressure()`：

```text
projected_input
= rendered system prompt
+ provider-side tool schema payload
+ canonical/model-visible history
```

Repo Map 已经嵌入 rendered system prompt，因此 `repo_map_tokens` 只单独记录用于诊断，不会再次加到 `projected_input`，避免重复计算。

新增 `history_limit_for_request()`：

```text
history_limit = min(
    legacy_history_budget,
    available_input - fixed_request_tokens,
)
```

因此 C2 的 request pressure 与最终 model-visible trim 使用同一预算语义。

## 2. `context/compaction.py`

新增独立 `CompactionEntry`：

- `checkpoint_id`
- `created_at`
- `summary_method`
- `summary_hash`
- `summary_text`
- `source_event_ids`
- `previous_checkpoint_id`
- `keep_recent_tokens`
- `retained_tail_tokens`

`TraceableCompaction` 维护 `entries` 与 `checkpoints` 两类证据。

关键语义变化：

```text
canonical ConversationHistory 不修改
        |
        +-> 计算 full request pressure
        +-> 生成 CompactionEntry
        +-> PrepareNextTurnResult(history_override=...)
        +-> 只影响下一次 LLM model-visible view
```

Repeated compaction 通过 `previous_checkpoint_id` 显式关联前一个 checkpoint，并且每次摘要都基于 canonical raw history 重新生成，不再 summary(summary(...))。

`CompactionCheckpoint` 新增：

- `previous_checkpoint_id`
- `projected_input_tokens`
- `projected_after_tokens`
- `available_input_tokens`
- `pressure_ratio`

当前 summary 方法仍为 `extractive-v1`，保留为 deterministic baseline；structured compaction 后置。

## 3. `agent/core.py`

Core 只做薄接线，不承担 Compaction 策略。

`PrepareNextTurnContext` 新增只读 request context：`system_content`、`repo_map_content`、`tool_schemas`。

`PrepareNextTurnResult` 新增：

```python
history_override: tuple[LLMMessage, ...] | None
```

语义：只对下一次模型调用可见，不写回 canonical ConversationHistory。

`_render_request_parts()` 统一生成 Repo Map cache、rendered system prompt 和 tool schemas；`_prepare_next_turn()` 与 `_build_messages()` 复用同一套 request parts。

`_build_messages()` 若存在 `history_override`：仅本次使用、使用后清空、再按 `history_limit_for_request()` 做最终防御性 TokenBudget trim；canonical history 完全不修改。

## 4. `tests/test_compaction.py`

覆盖：

1. ContextPressure 计算 system / tool schema / history，并确认 Repo Map 不重复计入 projected total；
2. Compaction 后 canonical history 内容完全不变；
3. token-based recent complete units 仍完整保留；
4. History 较小时，只要完整 request 压力足够高，也可以触发 Compaction；
5. repeated compaction 的 `previous_checkpoint_id` 正确连接；
6. canonical history 中不会出现 `[Compacted earlier context ...]`；
7. Agent turn-boundary 集成测试确认 compacted view 只出现在第二次真实 model call；
8. Chat Session checkpoint 持久化继续兼容新增字段。

测试中的低 threshold 仅用于 deterministic contract test，不能作为未来正式 benchmark 的策略参数。

## 当前提交范围

C2 开始前基线：`fbf1eee46479b39703f1bf87fea840ba7b0cbcb9`。

C2 代码与测试完成点：`ab96536358bda8985a8985e60d2579e36767f595`。

GitHub compare 确认代码变化限定在用户确认的 4 个文件。`agent/core.py` 因 GitHub Contents API 重写时行尾/格式归一化，compare 显示的 additions/deletions 较大；验收以 pytest 行为和具体逻辑为准。

## 验证结果

用户已在本地 WSL/项目虚拟环境完成两组 pytest，结果全部通过：

```bash
python -m pytest -q tests/test_compaction.py
```

以及：

```bash
python -m pytest -q tests/test_chat.py tests/test_session_store.py tests/test_day2.py
```

因此 C2 于 2026-09-15 正式验收通过。

## C2 完成后的直接提升

| C1 | C2 |
| --- | --- |
| 触发只看 History 子预算 | 看完整下一次 request pressure |
| Repo Map / system / tools 不参与触发 | 全部纳入压力判断 |
| summary 写回 canonical History | canonical History 不被 summary 污染 |
| summary 看起来像真实 user message | 独立 `CompactionEntry` |
| repeated compaction 可能 summary 套 summary | 每次基于 raw canonical history，checkpoint 前后继显式关联 |
| checkpoint 主要记录 history before/after | 同时记录 projected input / after / available / pressure |
| TokenBudget 固定 history slice | 根据 fixed request 占用动态收缩 history 上限 |

## 仍未解决的边界

C2 之后仍有：

1. `extractive-v1` 仍不是结构化语义摘要；
2. 尚未先做 deterministic Tool-output pruning；
3. checkpoint 的 repo revision 仍只看 HEAD；
4. Session resume / 新 round 的 step 1 尚无 Context preflight；
5. `event_ref` 仍主要用于人工/Trace 审计，不是 Agent 自主 recall。

下一步进入 C3 文件范围确认；在用户确认前不修改 C3 生产代码。
