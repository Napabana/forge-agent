# Context Compaction C3 改动内容

日期：2026-09-15

## 本轮目标

C3 不再做泛化的“所有 step 1 preflight”，而是收口两个明确问题：

1. 统一 Context checkpoint 与 Chat Repo Map 对仓库状态的理解：`HEAD + working-tree snapshot`；
2. 对已有共享历史的 continuation run，在本轮第一次 LLM 调用前执行一次 Context policy，使 Chat Round 2+ / resume long session 不必先完成一个 Tool turn 才能 compact。

Fresh 单次任务 / Fresh Chat Round 1 只有当前 user message，不进入 round-boundary preflight。

本轮不做：deterministic Tool-output pruning、structured semantic summary、context recall、benchmark、provider-native compact。

## 实际修改范围

确认范围原本允许 7 个文件；实施后进一步收缩，实际代码/测试只修改 5 个文件：

- 新增 `context/repository_state.py`
- `context/compaction.py`
- `agent/runner.py`
- `entry/chat.py`
- `tests/test_compaction.py`

没有修改：

- `agent/core.py`
- `agent/session.py`
- `tests/test_chat.py`
- `config/default.yaml`

## 1. 统一 Repository Fingerprint

新增 `context/repository_state.py::repository_fingerprint()`：

```text
repository_fingerprint
= git HEAD
+ snapshot_repository(working tree)
```

其中 working-tree snapshot 直接复用 `agent.loop_detector.snapshot_repository()`，不复制第二套扫描逻辑。

`entry/chat.py::_repository_revision()` 现在只做薄代理，调用共享 helper；`context/compaction.py` checkpoint 也使用同一 helper。

因此 HEAD 不变但 tracked/untracked working tree 改变时，Chat Repo Map invalidation 与 Compaction checkpoint 会看到相同的 repo state 变化。

## 2. Round-boundary Preflight

实现位置放在 `agent/runner.py`，没有修改 Agent 主循环。

Runner 在进入 `Agent.run()` 前检查：

```text
history is not None
AND history.message_count > 1
AND prepare_next_turn callback exists
```

只有满足这些条件，才执行一次 shared-history boundary preflight。

因此：

```text
Fresh Chat Round 1
user message only
-> no preflight
-> Agent step 1

Chat Round 2+ / Resume
prior canonical history + current user message
-> round-boundary preflight
-> pressure low: no-op
-> pressure high: history_override
-> first LLM call
```

preflight 复用 C2 已有的：

- `Agent._render_request_parts()`
- `PrepareNextTurnContext`
- `PrepareNextTurnResult.history_override`
- `TraceableCompaction`

没有新增第二套压缩策略。

Agent 内部原有生命周期保持不变：

```text
完整 Tool turn -> step > 1 -> prepare_next_turn -> next model call
```

## 3. Checkpoint Lineage 跨 Session Resume

`TraceableCompaction` 新增轻量 lineage cursor：

- `restore_checkpoint_lineage(checkpoint_id)`
- `reset_checkpoint_lineage()`

Resume 时只恢复最后一个 `checkpoint_id`，不恢复旧 summary，也不把 summary 写入 canonical history。

`entry/chat.py` 在：

- resume：从 persisted `compaction_checkpoints[-1].checkpoint_id` 恢复 cursor；
- clear history：重置 cursor；
- start new session：重置 cursor 与当前进程待持久化 checkpoint；
- 新建 fresh ChatSession：重置 cursor。

这样下一次 checkpoint 的 `previous_checkpoint_id` 可以跨进程连接，但 Context view 仍从完整 canonical history 重新计算。

## 4. C2 语义保持不变

C3 没有重新引入 destructive history mutation：

- Compaction 不调用 `history.replace()`；
- compact marker 只存在于一次性 `history_override`；
- canonical `ConversationHistory` 保留真实 user / Action / Observation；
- 当前 round user message仍只追加一次。

## 5. 新增测试契约

`tests/test_compaction.py` 增加：

1. HEAD 不变、working tree 改变时 repository fingerprint 必须变化；
2. Chat `_repository_revision()` 与共享 `repository_fingerprint()` 结果一致；
3. checkpoint `repo_revision` 使用共享 fingerprint；
4. lineage cursor 可 restore/reset，且不会恢复旧 summary；
5. Fresh Chat Round 1 不做 boundary compaction；
6. 已有长 history 的新 round，第一次真实 LLM call 就能看到 compacted view；
7. canonical history 不出现 compact marker，当前 user message 只出现一次；
8. persisted Session resume 后，新 checkpoint 的 `previous_checkpoint_id` 指向恢复前最后 checkpoint；
9. C2 原有 step>1 turn-boundary compaction 测试继续保留。

## 当前提交

C3 实现前基线：

`451b03f0c8d0d7c6e59707191dd88124c1f2616d`

C3 代码与测试完成点（创建本文档前）：

`12d758401e86321d55187d1de9ce20920d72cf3a`

C3 交接文档提交点：

`2e92eb579a2bf3409a121e13ed48d83ad9e2ea49`

## 验证状态

用户已在本地 WSL 环境运行并确认以下两组测试全部通过：

```bash
python -m pytest -q tests/test_compaction.py
```

以及：

```bash
python -m pytest -q \
  tests/test_chat.py \
  tests/test_session_store.py \
  tests/test_day2.py
```

因此 C3 已实现的 repository fingerprint、Runner shared-history boundary preflight、checkpoint lineage 与既有 Chat/Session 回归均已通过本地 pytest 验证。

## 生产入口核对发现的剩余缺口

在进入 C4 前重新核对真实 CLI 入口时发现：`entry/cli.py` 创建 `ChatSession` 时当前没有传入 `prepare_next_turn`，因此普通用户直接执行 `agent chat` 时，`ChatSession._prepare_next_turn` 仍为 `None`。

这意味着：

- C1~C3 的 Context policy 代码与定向集成测试均已通过；
- 但真实 `agent chat` CLI 入口尚未实例化/注入 `TraceableCompaction`；
- 因而 production Chat 目前不会实际触发 C3 round-boundary preflight。

该问题属于 C3 production wiring 漏项，不应被 C4 的 pruning 逻辑掩盖。进入 C4 生产实现前应先单独补 `entry/cli.py` 的一处策略注入，并做 CLI/Chat 回归。

## 已知边界

1. Round-boundary preflight 发生在 Runner 调用 `Agent.run()` 之前，因此 `CONTEXT_COMPACTED` Trace 可能早于本轮 `TASK_START` 事件；当前 EventLog 没有依赖 TASK_START 必须第一条的契约。若后续 benchmark 需要严格生命周期顺序，应再增加显式 round-preflight event/phase，而不是改变现有 TASK_START 语义。
2. 当前 summary 仍是 deterministic `extractive-v1`；压缩质量属于 C4/C5。
3. Round-boundary preflight 与 step>1 prepare 可能在同一 run 中产生连续 checkpoint；canonical history 不受影响，是否需要减少重复压缩由后续 benchmark 决定。
4. `event_ref` 仍是 Trace 审计能力，不是 Agent 自主 recall。
