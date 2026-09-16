# Context Compaction C3 设计收口

日期：2026-09-15

## 结论

C3 不再设计成“所有 Agent step 1 前都做 preflight”，而是明确收敛为：

> 只对已经存在 prior shared history 的 Chat 新 round / resume round，在该 round 第一次 LLM call 前执行一次 ContextPressure preflight。

全新单次 Agent task 与全新 Chat 第一轮不需要该 preflight。

## 为什么需要 round-boundary preflight

当前 `prepare_next_turn` 只在 `Agent.run()` 内 `step > 1` 时触发，因此它解决的是：

```text
Tool turn 完成
-> prepare_next_turn
-> 下一 Agent step
```

但 Chat 是跨 round 共享 canonical history 的。Round 2、Round 10 或 `/resume` 后的新 round，虽然新的 `Agent.run()` 从 `step=1` 开始，但输入可能已经包含很长的旧 Session history。

因此目前链路是：

```text
长 shared history
+ 本轮 user message
-> round step 1
-> 只能依赖 TokenBudget trim
-> 第一次 Tool turn 后
-> step 2 才可能 Compaction
```

C3 要补的是 round boundary，不是修改普通 Agent 的首步语义。

## 最终生命周期

```text
Fresh Agent task
  -> no round preflight
  -> step 1

Fresh Chat Round 1
  -> no prior history
  -> no round preflight
  -> step 1

Chat Round 2+ / Resume Round
  -> detect prior canonical history
  -> append latest user message exactly once
  -> Runner(round_boundary_preflight=True)
  -> Agent builds real request parts
  -> ContextPressure check
      -> low pressure: no-op
      -> high pressure: CompactionEntry + one-shot history_override
  -> first LLM call of the round
  -> later Tool turn
  -> existing step>1 prepare_next_turn lifecycle remains unchanged
```

## 为什么 preflight 由 RunRequest 显式控制

不采用以下方案：

```text
Chat 直接写 Agent._prepared_history_override
```

原因：

- 依赖 Agent 私有状态；
- 依赖 Runner 是否重建 Agent 的隐藏实现；
- 取消/失败时容易留下 stale override；
- Chat 会开始承担 Context policy 生命周期。

最终设计：

```text
RunRequest.round_boundary_preflight = False  # 默认
```

只有 Chat 有 prior shared history 时设为 True。Runner 只透传，策略仍由 Agent/context 层执行。

## Agent 侧设计

`Agent.run()` 增加默认关闭的 round-boundary preflight 参数。

每次 run 开始必须先清空旧的一次性 override，避免上一个 run 中断后把 model view 泄漏到下一 run。

内部将 round/turn 两种 boundary 的公共部分抽成一个薄 context-policy 调用层：

```text
round boundary
  -> optional, before step loop

turn boundary
  -> existing step > 1 branch
```

二者都复用：

- `_render_request_parts()`；
- `PrepareNextTurnContext`；
- 同一个 `prepare_next_turn/context policy` callback；
- `PrepareNextTurnResult.history_override`。

`PrepareNextTurnContext` 增加 boundary/phase 标记，例如：

```text
round
turn
```

这样 Trace 与策略能区分“跨 Chat round 的预检”和“Tool turn 后的下一步准备”。

## Chat 侧触发条件

必须先判断 prior context，再追加新用户消息：

```text
had_prior_context = len(shared_history) > 0
append current user message
```

然后：

```text
RunRequest(
    ...,
    round_boundary_preflight=had_prior_context,
)
```

原因：

- Fresh Round 1 原本 history 为空，不做 preflight；
- Round 2+ 与 resume 自然为 True；
- 最新 user message 已经进入 canonical history，因此 pressure 和 recent tail 都包含本轮真实需求；
- 不需要为 `/resume` 写特殊分支。

## RepositoryState 统一

当前两个语义不一致：

```text
Compaction checkpoint: HEAD only
Chat Repo Map invalidation: HEAD + snapshot_repository
```

C3 新增：

```text
context/repository_state.py
repository_fingerprint(repo_path)
```

统一语义：

```text
HEAD + working-tree snapshot
```

其中 working-tree snapshot 复用现有 `agent.loop_detector.snapshot_repository()`，不复制扫描逻辑。

Chat 与 Compaction checkpoint 都改用该 helper。

Session 当前字段名 `repo_revision` 保持兼容，不做 schema/version bump；只是值的语义升级成统一 fingerprint。

## Resume 与 checkpoint lineage

C2 中 `previous_checkpoint_id` 目前依赖当前进程内的 `TraceableCompaction.entries`。进程重启后，这个内存状态会消失。

C3 不需要恢复旧 summary 作为事实，因为 canonical history 仍完整存在。只需恢复 lineage cursor：

```text
last persisted checkpoint id
-> TraceableCompaction active previous_checkpoint_id
```

因此建议提供轻量接口：

```text
restore_checkpoint_lineage(...)
reset_checkpoint_lineage()
```

Chat 行为：

- `_restore_session()`：从 persisted compaction checkpoints 恢复最后 checkpoint id；
- `start_new_session()`：重置 active lineage；
- `clear_history()`：重置 active lineage，防止新的 context checkpoint 继续连接到已经清空的旧上下文。

旧 checkpoint 仍可保留在 Session state 作为审计记录，不需要删除。

## 失败语义

round-boundary preflight 不应静默失败后换成另一套未记录 Context 行为。

因此沿用现有 prepare callback 的保守语义：

```text
context policy exception
-> before any LLM/tool call
-> current round fails
```

同时 C2 已保证 Compaction 不修改 canonical history，因此异常/取消不会留下半写 summary。

低 pressure 时不生成 checkpoint，也不制造无意义 Trace。

## C3 修改范围

生产/测试范围：

1. 新增 `context/repository_state.py`
2. `context/compaction.py`
3. `agent/core.py`
4. `agent/runner.py`
5. `entry/chat.py`
6. `tests/test_compaction.py`
7. `tests/test_chat.py`

明确不修改：

- `agent/session.py`
- `config/default.yaml`
- structured summary
- deterministic pruning
- context recall
- benchmark

`agent/runner.py` 现在确定需要修改，因为显式 `RunRequest.round_boundary_preflight` 比 Chat 直接操作 Agent 私有 override 更稳定，也不依赖 Runner 是否重新构造 Agent。

## 测试矩阵

### Repository fingerprint

- HEAD 不变，tracked file 修改 -> fingerprint 改变；
- HEAD 不变，untracked file 新增 -> fingerprint 改变；
- unchanged repo -> fingerprint 稳定；
- Compaction checkpoint 与 Chat 使用同一 fingerprint。

### Fresh round

- Fresh Chat Round 1：不执行 round-boundary preflight；
- 不产生 Compaction checkpoint；
- 第一次模型调用保持正常。

### Round 2+

固定 seeded long canonical history：

- Round 2 第一次 model call 前已经 pressure check；
- 高 pressure 时第一次 model call 直接看到 compacted view；
- canonical history 不包含 compact marker；
- 当前 user message 只出现一次。

### Resume

- 保存 long Session；
- 新进程/新 ChatSession 恢复；
- fresh `TraceableCompaction` 恢复 previous checkpoint lineage；
- resume 后新 round 第一次 model call 即可 compact；
- 新 checkpoint `previous_checkpoint_id` 指向 persisted last checkpoint。

### Reset

- `start_new_session()` 后 lineage 为空；
- `clear_history()` 后后续 checkpoint 不连接已清空上下文。

### Regression

- 当前 `step > 1 -> prepare_next_turn` Tool-turn 测试继续通过；
- `tests/test_chat.py` 现有跨轮 history 与 Repo Map cache 测试继续通过；
- C1/C2 `tests/test_compaction.py` 全部继续通过。

## C3 完成后的设计边界

C3 完成后只解决：

```text
正确的触发时机
+ 一致的 repository state
+ resume checkpoint lineage
```

不会让 `extractive-v1` 变聪明，也不会降低旧 Tool output 的 token 成本。

真正的内容质量优化仍属于：

```text
C4 deterministic tool-output pruning
C5 structured compaction
```

因此 C3 后不应提前声称 Compaction 已达到最终语义质量。
