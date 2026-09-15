# Context Compaction C1 改动内容

日期：2026-09-15

## 本轮目标

按已确认的 C1 范围先收口 Context 的三个基础矛盾：

1. `ConversationHistory` 不再在 Compaction/TokenBudget 之前按消息数永久删除旧证据；
2. TokenBudget 与 Compaction 复用同一套 `HistoryUnit`，Action/Observation 不再各自维护配对规则；
3. Compaction 的 recent tail 从固定消息数改为 token-based，并按完整 HistoryUnit 保留。

本轮不开始 C2，不实现 full request pressure、独立 CompactionEntry、structured semantic summary、Session preflight、context recall 或 benchmark。

## 实际修改

### `context/history.py`

- `ConversationHistory` 改为保存 canonical logical history。
- `max_messages` 参数继续保留作为旧配置兼容提示，但不再触发 destructive message-count trim。
- `add()`、`add_many()`、`replace()` 不再调用 `_trim()`。
- 真正发给模型的窗口继续由 TokenBudget/后续 Context policy 控制。

### `context/token_budget.py`

- 将原私有 `_HistoryUnit` 提升为共享 `HistoryUnit`；保留 `_HistoryUnit` 别名避免立即破坏内部兼容。
- 新增 `history_units()`，统一 Action/Observation 配对语义。
- 新增 `history_unit_tokens()`，统一单元 token 成本计算。
- 新增 `recent_history_units()`，从最新历史向前按 token budget 保留完整 unit；最新 unit 即使单独超过预算也不拆开。
- `TokenBudget._conversation_units()` 和 DP 成本计算改为复用上述共享语义，其余预算分配和 trim 算法不变。

### `context/compaction.py`

- `TraceableCompaction(retained_tail=...)` 改为 `keep_recent_tokens=...`，默认 8,000 tokens。
- 使用 `recent_history_units()` 决定 retained tail，不再按固定消息数切边界。
- checkpoint 继续保留兼容性的 `retained_tail` 消息数，并新增 `keep_recent_tokens`、`retained_tail_tokens`，便于后续实验重算。
- 触发条件仍是现有 history-budget pressure；full request pressure 留到 C2。
- `extractive-v1` 摘要算法未升级；structured compaction 留到后续批次。

### `tests/test_compaction.py`

新增/调整契约：

- `max_messages=3` 时仍保留完整 logical history，证明 History 不再抢先丢证据；
- 统一 `HistoryUnit` 能保持 assistant Action + user/tool Observation 配对；
- Compaction 按 token budget 保留最近两个完整 unit，不产生 orphan；
- checkpoint 记录 token-based tail 字段；
- 原 turn-boundary Trace 与 Chat Session checkpoint 测试改用 `keep_recent_tokens`。

## 范围核对

实现前确认点：`3a8d3409df35346e046daf7acf413e494fed5562`。

代码完成时 HEAD：`8c424e152b251217efb84181bde889d0c5d6b2d9`。

GitHub compare 显示这 4 个 commit 只修改：

- `context/history.py`
- `context/token_budget.py`
- `context/compaction.py`
- `tests/test_compaction.py`

没有修改 `agent/core.py`、`entry/chat.py`、Session 生产代码、provider、evals benchmark 或 `config/default.yaml`。

## 验证

当前 ChatGPT 执行环境没有用户 WSL 工作树；尝试从 sandbox clone GitHub 时因无法解析 `github.com` 未能建立完整 pytest 运行环境，因此本轮不能声称 pytest 已通过。

已完成的本地静态/逻辑检查：

- 4 个本轮 Python 文件内容均通过 Python `ast.parse` 语法检查；
- 对更新后的 `history_units()` / `recent_history_units()` / `TraceableCompaction` 做轻量隔离逻辑检查：最近两个完整 unit 被保留，前 3 个 unit 的 event refs 进入 checkpoint，`retained_tail_tokens == keep_recent_tokens` 在精确两-unit 预算样例中成立。

用户 pull 后应在 WSL 运行：

```bash
cd /mnt/e/2806/forgeAgent/forge-agent
source ~/.venvs/forge-agent/bin/activate
python -m pytest -q tests/test_compaction.py
```

由于 `ConversationHistory` 从 destructive window 改为 canonical store，若定向节点全部通过，再建议补一组受影响回归：

```bash
python -m pytest -q tests/test_session_store.py tests/test_chat.py
```

不要在未运行这些命令前把 C1 表述为“pytest 已通过”。

## 当前提高

C1 只完成 Context 语义基础收口，不声称 Token/成功率收益：

- 旧设计：40 条窗口可能在 Compaction 前永久丢历史；新设计：canonical history 先保留，模型可见窗口再由 Context policy 控制。
- 旧设计：History、Compaction、TokenBudget 对 turn 边界定义不一致；新设计：TokenBudget 与 Compaction 共用 HistoryUnit。
- 旧设计：recent tail 固定 N 条消息；新设计：按 N tokens 保留最近完整 Tool turn。

## 已知边界与下一步

C1 仍保留三个明确边界：

1. Compaction 触发仍只看 history 子预算，不是完整 request pressure；
2. summary 仍是 `extractive-v1`，并继续作为普通 user message 写回 History；
3. checkpoint repo revision 仍只记录 HEAD。

这些属于 C2/C3，不在本轮顺带修改。

下一步只有在 C1 的 WSL 定向测试真实通过后，才进入 C2。C2 开工前仍需按 `AGENTS.md` 列出拟修改文件和理由并等待用户确认。
