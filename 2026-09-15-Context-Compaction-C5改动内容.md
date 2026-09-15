# Context Compaction C5 改动内容

日期：2026-09-15
分支：`dev`

## 1. 状态

C5 Hybrid Structured Compaction 代码阶段已完成，当前等待用户本地 pytest 验收。

C1~C4 已由用户本地 pytest 验收通过；C5 在测试通过前不标记为正式完成。

## 2. 本轮目标

C5 将 C4 Stage B 的简单历史压缩替换为 Hybrid Structured Compaction：

- Tool / test / file / event_ref 等协议事实由 deterministic code 提取；
- Hard Constraints / Decisions / Progress / Next Actions 等自然语言语义只在 Stage B 真正触发时额外调用一次 LLM；
- 中文、英文、混合语言不依赖关键词 regex；
- canonical `ConversationHistory` / EventLog 保持事实源，summary 只影响 model-visible view；
- semantic compaction 开始前通过 EventLog 向 Chat 前端显示 `[压缩上下文]`；
- semantic side-call token 与 latency 可追踪；
- active compacted view 避免同一 Task 的后续 step 重复调用 summary model。

## 3. 实际修改范围

### 生产代码

1. 新增 `context/history_evidence.py`
   - 统一解析 Forge 自己的 `Action:` / `Params:` / `[Tool: ... | SUCCESS/ERROR]` grammar；
   - 提供 Action/Observation pair、稳定 fingerprint；
   - `is_user_authored_message()` 排除 Tool Observation；
   - 额外排除 Forge 自动注入的 `[REFLECTION]` prompt，避免把 Agent recovery 指令误识别成用户约束。

2. `context/tool_pruning.py`
   - C4 pruning 改为复用 `history_evidence.py`；
   - pruning 规则本身保持不变。

3. 新增 `context/structured_compaction.py`
   - `SemanticContextFields`；
   - `DeterministicEvidence`；
   - `StructuredContextState`；
   - `LLMSemanticSummarizer`；
   - 内部-only `record_context_summary` Tool Schema；
   - semantic packet 构造；
   - deterministic evidence extraction；
   - safe fallback；
   - bounded renderer。

4. `context/compaction.py`
   - Stage A pruning 后仍高压才进入 Stage B；
   - semantic call 前写 `CONTEXT_COMPACTION_STARTED`；
   - semantic failure 写 `CONTEXT_COMPACTION_FAILED`；
   - 成功为 `structured-hybrid-v1`；
   - failure/no summarizer 为 `structured-fallback-v1`；
   - summary usage / semantic duration 写 checkpoint 与 Trace；
   - 维护进程内 `ActiveCompactedView`；
   - active view 只在同一 `task.description` 内复用，新 Chat round Goal 变化时自动失效；
   - re-compaction 仍从 canonical history 重新构造，不做 summary-of-summary。

5. `agent/task.py`
   - 新增 `CONTEXT_COMPACTION_STARTED`；
   - 新增 `CONTEXT_COMPACTION_FAILED`。
   - 说明：GitHub Contents API 写回时正规化了原文件换行，所以 compare 里该文件显示大量 additions/deletions；实际语义改动仅上述 EventType。

6. `entry/chat.py`
   - ChatSession 初始化时把真实 backend 绑定给 Context policy；
   - started event 显示 `[压缩上下文]`；
   - failed event 显示 `[压缩上下文失败，使用安全回退]`；
   - semantic side-call usage 在 round 结束前 merge 到 `RunResult.usage`，再进入 SessionUsage；
   - 中断路径也 consume 已发生的 summary usage，避免统计泄漏。

### 测试

7. 新增 `tests/test_structured_compaction.py`
   - parser；
   - multilingual semantic packet；
   - internal ToolCall structured output；
   - malformed response fallback；
   - deterministic verification / working set；
   - old test failure 被 later pass supersede；
   - bounded renderer；
   - started event ordering；
   - active view reuse；
   - semantic failure fallback；
   - `[压缩上下文]` UI；
   - summary usage merge。

8. `tests/test_tool_pruning.py`
   - 更新 Stage B 断言到 C5 structured summary；
   - Stage A pruning-only 契约保持。

## 4. 为什么最终没有修改 core / runner / cli

设计阶段预计可能需要修改：

- `agent/core.py`
- `agent/runner.py`
- `entry/cli.py`

实现时进一步收口后发现不需要：

- semantic side-call usage 由 `TraceableCompaction` 自己累计；
- `ChatSession` 在每轮结束时统一 merge，因此 round-boundary 与 turn-boundary 都能覆盖；
- C4 已经让真实 `agent chat` 创建会话级 `TraceableCompaction()`，C5 只需在 `ChatSession` 初始化时调用 policy 的 `bind_backend()`；
- 因此无需把 memory-maintenance 细节侵入 Agent 主循环或 Runner。

## 5. 当前运行链路

```text
canonical history
      |
      v
full request pressure
      |
      v
C4 deterministic tool pruning
      |
      | relief enough
      +-----------------> pruning-only view
      |
      v
[压缩上下文]
      |
      v
semantic summary LLM call
      + deterministic evidence
      |
      v
Structured Context State
      |
      v
summary + recent raw tail
      |
      v
normal Agent LLM call
```

## 6. Semantic / Deterministic 边界

Semantic LLM 只负责：

- Hard Constraints
- Decisions
- Completed
- In Progress
- Blocked（语义层）
- Next Actions

程序负责且 LLM 无权覆盖：

- 当前 Goal：`task.description`
- Verification State
- unresolved Tool failures
- read / modified paths
- event_ref
- repository current truth

## 7. Active Compacted View

第一次 semantic compaction 后保存进程内 active view：

```text
summary + canonical raw delta
```

同一 Task 的后续 step 先计算这个 view 的 pressure：

- 仍低于 threshold：直接复用，不重新调用 summary model；
- 再次高压：重新从 canonical history compact。

Chat 新 round 的 `task.description` 会变化，因此旧 active view 立即失效，避免摘要里的 `Goal` 陈旧。

Resume V1 仍不恢复 active summary；首次再次高压允许重新 semantic compact。

## 8. 前端可见事件

semantic call 真正开始前：

```text
EventType.CONTEXT_COMPACTION_STARTED
```

Chat 显示：

```text
[压缩上下文]
```

如果 semantic extraction 失败：

```text
EventType.CONTEXT_COMPACTION_FAILED
```

Chat 显示：

```text
[压缩上下文失败，使用安全回退]
```

成功后继续写已有 `CONTEXT_COMPACTED` checkpoint，不重复打印成功提示。

## 9. Usage / Trace

semantic side-call 的 `TokenUsage` 由 policy 累计，并在 Chat round 结束前 merge 到该轮 `RunResult.usage`。

Checkpoint / Trace 记录：

- `summary_usage`
- `semantic_duration_ms`
- `semantic_packet_truncated`
- `semantic_error`
- before / after tokens
- projected pressure
- source event ids
- previous checkpoint lineage

因此后续 B1 可以区分普通 Agent 调用和 compaction 调用的成本。

## 10. Safe fallback

semantic ToolCall malformed / provider failure 时：

- canonical history 不变；
- 写 `CONTEXT_COMPACTION_FAILED`；
- 不把未知自然语言强行 regex 分类；
- 使用 current Goal + deterministic evidence + bounded user-authored excerpts；
- summary method 为 `structured-fallback-v1`；
- 最终仍有 TokenBudget trim 作为硬兜底。

## 11. 未修改范围

本轮没有修改：

- `agent/core.py`
- `agent/runner.py`
- `agent/session.py`
- `entry/cli.py`
- `context/token_budget.py`
- `tools/*`
- provider backend 实现
- `config/default.yaml`

## 12. 验收命令

第一组：

```bash
python -m pytest -q \
  tests/test_structured_compaction.py \
  tests/test_tool_pruning.py \
  tests/test_compaction.py
```

第二组回归：

```bash
python -m pytest -q \
  tests/test_chat.py \
  tests/test_session_store.py \
  tests/test_day2.py
```

测试通过前，本文件只记录“代码完成、待验收”，不得写成 C5 已正式完成。

## 13. 后续

C5 验收后，Context Compaction 主实现 C1~C5 收口。

下一阶段优先进入 B1 Context Policy 离线 benchmark，不直接实现 C6；只有 benchmark 出现稳定“必须回查旧 Tool 原文”的失败样本时，再决定是否实现 `context_recall(event_ref)`。
