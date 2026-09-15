# Forge Agent Context Compaction 执行方案

日期：2026-09-15

冻结分析基线：`dev@a86c4e48f3cd4235866371d8f534dc346e93c905`

目标不是继续堆功能，而是先把当前 Context/Compaction 的职责收口，再进行可复现 benchmark。正式结果必须能从固定任务、原始 Trace 和隐藏 verifier 重算，不用测试数量代替真实任务效果。

## 1. 当前实现总结

当前上下文实际上有三套相互独立的裁剪机制：

1. `context/history.py`
   - `ConversationHistory(max_messages=40)` 默认按消息条数限制。
   - 超限后直接从 index 1 开始永久删除旧消息，只保证第一条任务消息保留。
   - 该层不理解 Action/Observation 配对。

2. `context/token_budget.py`
   - 真正发给模型前按 token 预算再次裁剪。
   - 已经能把 assistant Action + user/tool Observation 视为不可拆分单元。
   - 该裁剪只影响本次 model-visible view，不修改 canonical history。

3. `context/compaction.py`
   - `TraceableCompaction` 通过 `prepare_next_turn` 在完整 Tool turn 后、下一次 `_build_messages()` 前执行。
   - 当前触发条件为 `history_tokens >= history_budget * threshold`。
   - 默认保留第一条消息和最近 `retained_tail` 条消息，中间历史用 `extractive-v1` 串接并按字符截断。
   - 压缩后直接 `history.replace(...)`，因此摘要被重新写回逻辑 History。
   - checkpoint 已记录 before/after token、source event IDs、HEAD、summary hash、retained tail；原始 Tool 结果仍在 EventLog。

当前版本的优点：
- Compaction 不直接侵入 `Agent.run()` 主循环，而是复用 `prepare_next_turn` 生命周期。
- 原始 Tool Trace 未因压缩丢失。
- checkpoint 已具备可追溯和可重算基础。

## 2. 当前需要先收口的核心矛盾

### 2.1 History Window 会在 Compaction 前永久丢历史

`ConversationHistory._trim()` 可能已经把最旧消息删除，Compaction 后续根本看不到这些内容，也无法形成摘要或 event_ref 覆盖。

因此当前链路实际是：

```text
History message-count destructive trim
    -> Compaction
    -> TokenBudget model-view trim
```

同一份上下文被三种不同规则连续处理，实验很难隔离 Compaction 自身收益。

### 2.2 三层对“turn/unit”的定义不一致

- History：按单条消息。
- Compaction：按固定消息 tail。
- TokenBudget：按 Action/Observation unit。

Coding Agent 中工具输出可能很长，因此“8 条消息”没有稳定 token 语义，也可能拆开工具交互。

### 2.3 Compaction 只看 History 压力，不看完整请求压力

当前触发条件只使用 history budget，但真实模型输入还包含：

```text
system + tool schemas + repo map + history + pending context + output reserve
```

因此 History 未到阈值时完整请求可能已经接近窗口；反过来固定 history 子预算达到阈值时，模型总窗口可能仍很宽裕。

### 2.4 `extractive-v1` 不是语义摘要

它只是把旧消息按 role 串起来再截断，不能稳定区分：
- 用户硬约束；
- 架构决策；
- 未解决失败；
- 最新验证状态；
- 已过时 repo/test 状态；
- 普通长 Tool 输出。

因此当前版本可以作为 deterministic baseline，但不宜直接作为最终 Context Compaction 设计。

### 2.5 Summary 被伪装成普通 user message

压缩结果重新放回 `ConversationHistory`，后续再次压缩时会把旧摘要当普通用户历史继续摘要，容易产生 summary-of-summary 漂移，也无法区分真实用户输入与 Harness 生成状态。

### 2.6 Repo revision 语义不统一

Compaction checkpoint 只记录 `git rev-parse HEAD`，而 Chat 的 Repo Map 刷新已经使用 `HEAD + working tree snapshot`。未提交修改是 Coding Agent 的常态，因此两处对“仓库是否变化”的判断可能相互冲突。

### 2.7 `event_ref` 当前只完成可审计，没有真正完成 Agent 可回查

压缩后原始 Tool 结果仍在 EventLog，人可以追溯；但 Agent 当前没有 `context_recall(event_ref)` 一类只读能力。因此当前只能准确表述为 traceable，不应提前表述为 agent-retrievable。

### 2.8 Session resume 第一轮不会执行 `prepare_next_turn`

`prepare_next_turn` 只在 `step > 1` 执行。恢复一个超长 Session 后，新一轮 step 1 会直接构建消息，只能依赖 TokenBudget 裁剪，直到第一次 Tool turn 后才可能 Compact。

不应破坏已固定的 `prepare_next_turn` 生命周期；应补充 Session round preflight，而不是让 step 1 也调用 prepare callback。

## 3. 目标架构

Context 分三层：

```text
Canonical State
  Session / EventLog / Repository
  完整、可审计，不因压缩丢失
          |
          v
Context State
  Anchors
  CompactionEntry
  Recent History Units
  event refs / working set
          |
          v
Model-visible View
  System
  Repo Map
  Hard Constraints / Anchors
  Compact Summary
  Recent Raw Turns
  Retrieved Context
```

核心原则：

> Compaction 不再修改事实本身，只改变下一次模型可见的 Context View。

这与 Forge 当前 Repo Map 的设计原则一致：Repo Map 是仓库事实的 model-visible representation，而不是仓库事实源。

## 4. 优化方案

### 4.1 统一 Canonical History 与 Model-visible View

`ConversationHistory` 不再承担长会话的 destructive compaction。Canonical History 应尽量保存完整逻辑对话；实际模型可见内容由 Context policy 决定。

第一阶段不要求迁移数据库，也不要求无限常驻内存；重点是停止在 Compaction 之前无证据地按消息数永久删除历史。

### 4.2 提取统一 HistoryUnit

将目前 `TokenBudget` 内部的 Action/Observation unit 语义提升为 Context 层共享概念。

必须保证：
- Action/Observation 不被拆开；
- recent tail、pruning、budget trim 共用同一 unit；
- hard constraints / standalone user messages 可以形成独立 unit。

### 4.3 触发依据改成完整 Request Pressure

不再只看 `history_tokens / history_budget`，改为估计下一次请求：

```text
projected_input = system + tool_schema + repo_map + context_view
available_input = total_budget - reserve
pressure = projected_input / available_input
```

第一版复用现有 `TokenBudget.total/default_plan().reserve` 即可，不急着引入模型元数据数据库。

### 4.4 Recent Tail 改成 token-based

将 `retained_tail=N messages` 改为 `keep_recent_tokens=N`，并按 HistoryUnit 从新到旧保留。

原因：工具 Observation 长度差异极大，固定消息数不能稳定代表模型上下文规模。

### 4.5 CompactionEntry 与真实 LLMMessage 分离

新增明确的 compaction state，不把摘要永久伪装成 `role=user` 原始历史。

建议字段：

```text
checkpoint_id
created_at
summary_method
summary_hash
summary_text / structured_summary
source_event_ids
source_unit_range
before_tokens
after_tokens
repo_state_hash
keep_recent_tokens
previous_checkpoint_id
```

`_build_messages()` 或 ContextManager 在生成 model-visible view 时再把 CompactionEntry 渲染成模型消息。

### 4.6 两阶段压缩

#### Stage A：Deterministic Pruning

先处理高 token、低长期价值、可回查的 Tool 结果，不调用额外 LLM。

优先目标：
- 旧 `file_read` 全文；
- 旧 shell stdout/stderr；
- 旧 pytest 长日志；
- 重复 search/find 结果；
- 已被后续状态覆盖的 Observation。

压缩后保留：

```text
Tool 类型
结果状态
关键错误/摘要字段
event_ref
full_output_available=true
```

该阶段必须按 HistoryUnit 整体处理。

#### Stage B：Structured Semantic Compaction

只有 Stage A 后仍超过压力阈值，才进行真正的语义摘要。

建议结构：

```text
Goal
Hard Constraints
Decisions
Progress
  - Completed
  - In Progress
  - Blocked
Unresolved Failures
Verification State
Working Set
Next Actions
Historical References
```

重要边界：Repo 当前事实不得由 summary 作为权威源；当前文件、测试、diff 必须按需重新读取。

第一轮实现可先提供 deterministic structured extractor；是否引入 LLM summarizer 必须由离线测试证明必要性后再决定。

### 4.7 统一 RepositoryState

复用 Chat 已有思路，统一成：

```text
HEAD + working-tree snapshot
```

Compaction、Session、Repo Map 后续都引用同一 repo state hash，避免 HEAD 未变但工作区已修改时状态错判。

### 4.8 保留 `prepare_next_turn`，补 Session Preflight

不改变既有语义：

```text
完整 Tool turn -> prepare_next_turn -> next Agent step
```

Chat 在 `/resume` 或新 user round 进入 Runner 前额外执行：

```text
ContextManager.preflight()
```

二者复用同一个 ContextManager/pressure policy。

### 4.9 `context_recall(event_ref)` 后置

作为第二阶段增强：
- 只读；
- 从 EventLog 回取原始 Observation；
- 经过现有输出截断/权限/Trace 管线；
- 不把 EventLog 变成隐式长期记忆搜索系统。

在该能力完成前，外部表述只能说“原始 Tool 结果可审计追溯”。

## 5. 与前沿设计的取舍

本方案只迁移公开、可验证的设计契约，不逐行复制实现：

- Pi：借鉴真实 context pressure、token-based recent tail、独立 CompactionEntry、结构化摘要和 previous-summary 语义。
- Anthropic/Claude Code 公开 Context Engineering：借鉴高信号 Context、Tool Output pruning、filesystem/trace just-in-time retrieval、结构化工作状态。
- OpenAI/Codex：认识 provider-native compaction 是未来可选能力，但 Forge 是 multi-provider，不把 OpenAI opaque compaction item 作为核心领域模型。

未来若需要，可增加：

```text
CompactionBackend
  - ForgeLocalStructuredCompaction
  - ProviderNativeCompaction
```

该接口后置，不属于当前 P1 收口。

## 6. 实施批次

必须按 `AGENTS.md`：每批修改生产代码前先列文件和理由，等待用户确认；只跑与风险相称的定向测试，不顺带扩展 MCP、多 Agent、multi-tool call、向量检索或 provider 协议。

### Batch C1：Context 所有权与 Unit 语义收口（第一批，待确认）

预计修改：

- `context/history.py`
  - 停止 Compaction 前的 destructive message-count trim，或提供 canonical 模式。
  - 不在此文件实现摘要策略。

- `context/token_budget.py`
  - 将 Action/Observation unit 抽成 Context 可复用的公开/内部稳定类型与 helper。
  - 保持现有 `trim_history()` 行为兼容。

- `context/compaction.py`
  - 将 fixed-message tail 迁到 token-based unit tail。
  - 暂不加入 LLM semantic summarizer。

- `tests/test_compaction.py`
  - 增加“旧 History 不会在 Compaction 前被 message window 抢先删除”。
  - 增加 Action/Observation 不孤儿、token-based tail 的确定性测试。

本批不改：`agent/core.py`、`entry/chat.py`、`agent/session.py`、provider、evals benchmark。

### Batch C2：Request Pressure + CompactionEntry

预计修改：

- `context/compaction.py`
- `context/token_budget.py`
- `agent/core.py`（只保留薄 Context View 组装接线）
- 必要时新增 `context/manager.py`
- `tests/test_compaction.py`

目标：
- full request pressure；
- 独立 CompactionEntry；
- summary 不再永久伪装为 user message；
- repeated compaction 可追溯。

### Batch C3：RepositoryState + Session Preflight

预计修改：

- 新增或提取统一 repo state helper；
- `context/compaction.py`
- `entry/chat.py`
- `agent/session.py` / session persistence tests

目标：
- checkpoint 使用 HEAD + working tree state；
- resume/new round 在 step 1 前执行同一 Context pressure/preflight；
- 不改变 `prepare_next_turn` 的既有 step>1 契约。

### Batch C4：Deterministic Tool-output Pruning

预计修改：

- `context/compaction.py` 或单独策略模块；
- Trace/EventLog 只读读取 helper（如确有必要）；
- 定向 tests。

目标：先减少低价值 Tool 输出，再决定是否摘要。

### Batch C5：Structured Compaction

先实现确定性结构化提取 baseline；只有固定离线 bad case 证明不足时，才增加 LLM summarizer。

必须保留 Goal / Hard Constraints / Decisions / Progress / Unresolved Failures / Verification / Working Set / Next Actions / Historical References。

### Batch C6：Optional Context Recall

只有前面 benchmark 证明压缩后确实存在“需要恢复早期原始细节”的稳定样本，才实现 `context_recall(event_ref)`。

## 7. 优化后的测试方案

### 7.1 第一层：Context Policy 离线测试

完全不调用真实模型。使用固定 prerecorded long history，建议 20~30 个完整 Tool turn、约 12k~20k estimated history tokens。

固定场景：

1. `early-hard-constraint`
   - 很早出现非首条用户硬约束。
   - 断言压缩后 constraint 仍进入 model-visible view。

2. `huge-tool-output`
   - 多个 3k~10k token Tool Observation。
   - 断言 pruning 显著降低 token，event_ref 完整保留。

3. `action-observation-pair`
   - 边界恰好落在工具 turn 中间。
   - 断言 orphan units = 0。

4. `superseded-state`
   - 早期 test A fail，后期 test A pass / test B fail。
   - 断言 model-visible state 不把 A 继续标记为 unresolved current failure。

5. `repeated-compaction`
   - 连续两次达到压力阈值。
   - 断言 previous checkpoint 有明确来源，真实用户消息与摘要状态不混淆。

6. `resume-long-session`
   - 保存长 Session 后恢复。
   - 断言第一轮 model call 前已执行 preflight，不依赖先完成一个 Tool turn。

7. `dirty-repo-revision`
   - HEAD 不变但 working tree 变化。
   - 断言 repo state hash 变化，checkpoint 不把旧工作区状态当当前状态。

离线指标：

```text
before_tokens
after_tokens
compaction_ratio
projected_input_tokens
context_pressure
hard_constraints_preserved
orphan_units
source_event_coverage
recent_tokens_kept
repo_state_hash_changed
checkpoint_atomic
raw_event_traceable
```

### 7.2 第二层：真实 Agent 消融

不要让模型随机跑 30 轮制造历史。每组从完全相同的 prerecorded long history 开始，然后让真实 Agent 继续完成最后一个 coding task。

正式三组：

```text
A. budget_trim_only
B. deterministic_pruning
C. hybrid_compaction
   deterministic pruning + structured compaction
```

旧 `message_window_40` 只作为 legacy baseline，可用于说明为什么旧设计被替换，不作为最终主对照。

建议真实模型 case：

1. early hard constraint：修 bug，但禁止改变 public API。
2. long tool output：真正根因位于早期长 pytest/shell 输出。
3. superseded state：早期失败已解决，后期有新的失败。
4. cross-file working set：最终任务依赖早期读到的接口关系。
5. double compaction + resume：两次 compaction、session save/resume 后继续完成任务。

隐藏 verifier 必须在 Agent History 外检查最终功能和硬约束。

### 7.3 Pilot 与正式重复

先跑：

```text
5 cases × 3 variants × 1 repeat = 15-run pilot
```

只验证数据管线和 case 难度，不把 pilot 百分比写简历。

确认无 fixture/trigger/trace 问题后，再跑：

```text
5 cases × 3 variants × 3 repeats = 45 runs
```

冻结：
- Forge commit；
- model/provider；
- prompt；
- tools；
- temperature/随机性配置；
- max_steps；
- context budget；
- prerecorded history；
- hidden verifier；
- 原始输出目录。

## 8. 正式指标

主指标只保留两个：

1. Hidden verifier pass rate
2. Provider input tokens per solved task

辅助指标：

- false-finish rate；
- compaction ratio；
- p50/p95 input tokens；
- max context pressure；
- tool calls；
- p50/p95 latency；
- context recall calls（若实现）；
- cached_tokens / input_tokens；
- cache_write_tokens；
- checkpoint count。

实际模型 Token 以 provider usage 为主，本地 tokenizer 估算只用于 Context pressure 和离线 policy 分析。

Prompt Cache 必须同时统计，因为 Compaction 改写前缀可能减少 input token，却降低 cache hit；最终不能只看 nominal token reduction。

## 9. 修改前 vs 目标设计

| 维度 | 当前 | 优化后 |
| --- | --- | --- |
| History 所有权 | message window 会永久删除 | canonical history 与 model view 分离 |
| Tool turn | 三层定义不一致 | 全部共用 HistoryUnit |
| 触发条件 | history 子预算阈值 | 完整 request pressure |
| Recent tail | 固定消息数 | 固定 token budget |
| 旧 Tool 输出 | 与普通文本一起摘要/截断 | 先确定性 pruning，保留 event_ref |
| Summary | role 文本串接 + 字符截断 | 结构化任务状态 |
| Summary 存储 | 伪装成 user message | 独立 CompactionEntry |
| 重复压缩 | summary-of-summary 风险 | previous checkpoint 明确关联 |
| Repo 状态 | 仅 HEAD | HEAD + working tree state |
| Resume | step 1 不 compact | Session preflight |
| Trace | 已可追溯 | 保留并强化 source-unit/repo-state 证据 |
| Recall | 人工可回查 | 后置为显式只读 event recall（有证据再做） |
| Benchmark | 容易混入 History Window 副作用 | 先离线 policy，再固定历史做真实 Agent 消融 |

## 10. 当前实施门禁

在用户确认 Batch C1 前：
- 不修改任何生产 Python 文件；
- 不写 Context benchmark 脚本；
- 不调用真实模型；
- 不顺带实现 LLM summary、context recall、MCP、多 Agent 或 multi-tool call；
- 不修改 `config/default.yaml`。

用户确认后，只执行 Batch C1，运行对应定向测试，汇报真实 diff 和结果，再决定是否进入 C2。
