# Context Compaction C5 设计收口（Hybrid v2）

日期：2026-09-15

## 1. 目标

C5 将 C4 Stage B 的 `extractive-v1` 替换为 **Hybrid Structured Compaction**：

- 能从 Forge 协议事实确定的信息，由程序 deterministic 提取；
- 必须理解自然语言语义的信息，只在真正发生 Stage B 压缩时额外调用一次当前 LLM；
- 不用中英文关键词正则去猜用户的 Goal / Hard Constraints / Decisions；
- 不在每轮 Agent step 注入 memory-maintenance prompt；
- canonical `ConversationHistory` / EventLog 仍是事实与审计源，summary 只是 model-visible Context State；
- semantic compaction 真正开始时，通过事件链向前端显示 `[压缩上下文]`。

核心原则：

> 能从协议事实确定的，绝不让 LLM 猜；必须理解自然语言语义的，绝不用 regex 硬猜。

## 2. 最终链路

```text
canonical history
      |
      v
full request pressure
      |
      | pressure < threshold
      +----------------------> no-op
      |
      v
Stage A deterministic tool-output pruning (C4)
      |
      v
recompute pressure
      |
      | pressure < threshold
      +----------------------> pruning-only model view
      |
      v
Stage B semantic compaction trigger
      |
      +--> EventLog: CONTEXT_COMPACTION_STARTED
      |        |
      |        +--> Chat/CLI/front-end 显示: [压缩上下文]
      |
      +--> deterministic evidence
      |
      +--> curated multilingual semantic packet
      |
      +--> one internal structured LLM call
      |
      +--> validate semantic fields
      |
      +--> merge deterministic + semantic state
      |
      +--> bounded renderer
      |
      +--> active compacted view + recent/raw delta
      |
      v
next normal Agent LLM request
```

Stage A pruning-only 不显示 `[压缩上下文]`。它是快速 deterministic cleanup；只有真正要发起额外 semantic summary LLM call 时显示，用户才能理解为什么当前回合出现短暂停顿和额外 token 消耗。

## 3. 为什么必须是 Hybrid，而不是纯正则

中文/英文/混合语言用户约束无法靠有限关键词可靠识别。例如：

```text
core 最好先别碰，优先放到 context 层，实在不行再改 Runner。
```

语义上包含：

- 暂不修改 `agent/core.py`；
- 优先在 Context 层实现；
- Runner 是 fallback。

它不一定包含 `must` / `do not` / `必须` / `不要` 等显式 marker。

因此 C5 不再设计“English regex + Chinese regex”的 Hard Constraint extractor。Regex 只用于解析 Forge 自己定义的稳定协议文本，例如：

```text
Action: file_write
Params: {"path": "context/compaction.py"}

[Tool: file_write | SUCCESS]
...
```

自然语言语义交给 LLM；Tool/Repo/Test 事实交给 deterministic code。

## 4. StructuredContextState

最终 model-visible state 固定为：

```text
Goal
Hard Constraints
Decisions
Progress
  Completed
  In Progress
  Blocked
Unresolved Failures
Verification State
Working Set
Next Actions
Historical References
```

建议数据结构：

```python
@dataclass(frozen=True)
class SemanticContextFields:
    hard_constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    completed: tuple[str, ...]
    in_progress: tuple[str, ...]
    blocked: tuple[str, ...]
    next_actions: tuple[str, ...]

@dataclass(frozen=True)
class DeterministicEvidence:
    unresolved_failures: tuple[str, ...]
    verification_state: tuple[str, ...]
    read_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    historical_references: tuple[str, ...]

@dataclass(frozen=True)
class StructuredContextState:
    goal: str
    semantic: SemanticContextFields
    evidence: DeterministicEvidence
```

`goal` 直接使用当前 `PrepareNextTurnContext.task.description`，不让 summary model 重写当前任务目标。

## 5. Deterministic Evidence：不交给 LLM 判断

### 5.1 HistoryEvidence 共享 parser

新增 `context/history_evidence.py`，统一解析 Forge 内部稳定 grammar：

- `ParsedAction`
- `ParsedObservation`
- `parse_action_message()`
- `parse_observation_message()`
- `Params:` JSON parser
- Action/Observation pair 校验
- deterministic action fingerprint

`context/tool_pruning.py` 与 C5 共用该 parser，避免多套 grammar 漂移。

### 5.2 Working Set

从 Action Params 确定性提取：

Read / inspect：

- `file_read.path`
- `file_view.path`
- `git_diff.path`
- search/find 的显式 `path`

Modified：

- successful `file_write.path`
- 可明确从 schema 得到的 git add/commit 路径

不从 shell command 自由文本猜文件修改。

### 5.3 Verification State

从完整 canonical history 判断最新 `test` / `pytest` Observation：

```text
PASS / FAIL / UNKNOWN
source event_ref
historical-only freshness warning
```

LLM semantic summary 无权覆盖 Verification State。

### 5.4 Unresolved Failures

先从 old region 找 failure，再用完整 canonical history（包括 recent tail）判断是否被后续成功 supersede。

特别是：

```text
old: pytest FAILED
recent: pytest PASSED
```

最终必须是 `PASS`，旧 failure 不进入 `Unresolved Failures`。

### 5.5 Historical References

checkpoint 保存完整 `source_event_ids`；model-visible summary 只保留关键 bounded refs。

C6 未实现前，`event_ref` 只是审计定位，不表示 Agent 能自主 recall。

## 6. Semantic Summary：只处理必须理解自然语言的字段

Semantic LLM 只负责：

- Hard Constraints
- Decisions
- Completed（自然语言层面的已完成事项）
- In Progress
- Blocked（语义 blocker；最终仍与 deterministic failure 合并）
- Next Actions

它不负责：

- 当前 Repo 内容；
- 当前 git status/diff；
- test PASS/FAIL 真值；
- read/modified path 的事实判断；
- event_ref 完整性。

## 7. 不新增“任意文本 LLM API”，复用现有 Tool Calling 抽象

C5 不新增 provider-specific JSON completion API。

新增一个内部-only Tool Schema，例如：

```text
record_context_summary(
  hard_constraints: string[],
  decisions: string[],
  completed: string[],
  in_progress: string[],
  blocked: string[],
  next_actions: string[]
)
```

`LLMSemanticSummarizer` 调用现有：

```python
backend.complete(messages, [record_context_summary_schema])
```

支持 Function Calling 的 backend 返回结构化 ToolCall params；不支持 Function Calling 的 OpenAI-compatible backend 已有文本 ToolCall fallback。

这个 ToolCall **只作为 structured-output transport，不进入 ToolRegistry，也不真正执行任何 Tool**。

必须校验：

- action 类型是 `TOOL_CALL`；
- tool name 正确；
- params 字段类型正确；
- 每个 list 有 item 上限；
- 单 item 有长度上限；
- 总 semantic output 有预算上限。

不合法就走安全 fallback，不把 malformed summary 写入 checkpoint。

## 8. Semantic 输入不是整份 raw history

Context 已经高压时，再把整份 history 原样送给 summarizer 会重复制造大请求。

C5 构造 curated semantic packet：

1. 当前 `task.description`；
2. old region 中所有 user-authored、非 Tool Observation 的自然语言消息（保留原语言）；
3. recent tail 中 user-authored natural messages，仅用于判断旧约束/决策是否已被用户修改；
4. deterministic evidence 的 compact representation；
5. 必要的历史 round-complete 摘要（bounded）；
6. 不发送 Repo Map、正常 Tool schemas、大块 Tool raw output、assistant exploration Thought 全文。

因此中文、英文和混合语言都由模型理解，而不是正则分类。

如果 semantic packet 自身超过 summary input budget：

- 保证当前 Goal 和最近 user-authored messages；
- old user messages 按消息边界 deterministic 截断；
- deterministic evidence 始终保留高优先级项；
- 记录输入被截断的 trace 字段；
- 不拆 Action/Observation unit。

## 9. Prompt 约束

Semantic compaction system prompt 必须明确：

- 只提取提供证据明确支持的事实；
- 不推断当前 repository/test/git 状态；
- 保留用户原始语言；
- 如果用户后续修改早期约束，以后续明确指令为准；
- 不把 assistant exploratory Thought 当成用户约束；
- 必须调用 `record_context_summary` 一次；
- 不输出额外 ToolCall。

## 10. Merge 优先级

最终 state 的优先级：

```text
Current task.description
        > deterministic protocol evidence
        > semantic extracted fields
        > fallback excerpts
```

例如 semantic model 输出 `tests pass` 也不会进入 Verification State；Verification 始终由 deterministic evidence 覆盖。

## 11. Active Compacted View：避免每一步多一次 summary LLM call

semantic compaction 不能在每次 `prepare_next_turn` 都重新调用模型。

`TraceableCompaction` 增加进程内 active state，例如：

```python
@dataclass(frozen=True)
class ActiveCompactedView:
    summary_text: str
    source_end_index: int
    source_prefix_hash: str
    checkpoint_id: str
```

第一次 Stage B：

```text
canonical prefix -> semantic compaction -> active summary
```

后续 turn：

```text
active summary
+ canonical messages[source_end_index:]
-> Stage A prune old delta where allowed
-> pressure check
```

若仍低于 threshold：

- 直接复用 active summary；
- 不调用 semantic model；
- 不生成新 semantic checkpoint。

只有 active summary + raw delta 再次达到 threshold 时才重新 semantic compact。

重新 compact 时仍从 canonical history 构造 curated semantic packet，**不把 previous summary 当输入事实源**，因此不形成 summary-of-summary drift。

Resume 后 V1 不要求恢复 `summary_text`；首次再次高压时允许重新做一次 semantic compaction。后续 benchmark 再决定是否值得持久化 active summary。

## 12. 前端可见 `[压缩上下文]`

当前已有 `EventType.CONTEXT_COMPACTED`，它是在压缩成功后写入。semantic LLM call 可能有明显延迟，因此仅在成功后显示不够。

C5 新增：

```text
CONTEXT_COMPACTION_STARTED = "context_compaction_started"
CONTEXT_COMPACTION_FAILED  = "context_compaction_failed"
```

当 Stage A 后仍高压、准备真正发起 semantic summary LLM call 时：

```python
log.log_trace(
    EventType.CONTEXT_COMPACTION_STARTED,
    step,
    mode="semantic",
    pressure_ratio=...,
    projected_input_tokens=...,
)
```

当前 Chat frontend `entry/chat.py::_print_event_live()` 收到后只显示一行：

```text
[压缩上下文]
```

不打印内部 summary，不污染正常回答。

外部 frontend / API 已经可以通过 EventLog / Runner `on_event` 收到该结构化事件，后续 UI 只需映射同一 event type。

成功后继续记录现有 `CONTEXT_COMPACTED`，包含 checkpoint 和 token evidence；不再额外打印第二行，避免 UI 噪音。

若 semantic summary 失败：

- Trace 写 `CONTEXT_COMPACTION_FAILED`；
- 前端可选显示 `[压缩上下文失败，使用安全回退]`；
- canonical history 不变；
- 不直接让 Agent task 因 summary API 失败而失败。

## 13. Usage / Token accounting 必须计入 summary LLM call

semantic summary 是真实 provider 调用，必须进入 Forge token usage。

扩展：

```python
@dataclass(frozen=True)
class PrepareNextTurnResult:
    ...
    additional_usage: tuple[TokenUsage, ...] = ()
```

Semantic summarizer 返回 `LLMResponse.usage`，`TraceableCompaction` 把它放进 `additional_usage`。

Turn-boundary：

- `agent/core.py::_prepare_next_turn()` 将 `additional_usage` 记录进当前 `SessionUsage`；
- 正常下一次 Agent LLM call 后，`total_tokens = usage.total_tokens` 自动包含 summary call。

Round-boundary：

- `agent/runner.py::_prepare_shared_history_boundary()` 收集 preflight `additional_usage`；
- `Agent.run()` 返回后 merge 到 `RunResult.usage`；
- Chat session 聚合时因此不会漏记 compaction 成本。

Trace 的 successful compaction payload 额外记录 `summary_usage`，benchmark 可区分 Agent 主调用和 compaction 调用成本。

## 14. Safe fallback

Semantic compaction 失败不能让整个 coding task 因 memory maintenance 直接失败。

fallback 顺序：

1. 若已有可复用 active compacted view，继续用 active summary + raw delta；
2. 否则构造 `structured-fallback-v1`：
   - current Goal；
   - deterministic evidence；
   - bounded user-authored raw excerpts（保留原语言，不做关键词分类）；
3. 再由最终 TokenBudget trim 做硬兜底。

fallback 不伪造 Hard Constraints/Decisions 分类；无法语义判断时保留 user excerpt 给主模型自己理解。

## 15. Bounded Renderer

最终 renderer 固定 section 顺序：

1. Goal
2. Hard Constraints
3. Decisions
4. Progress
5. Unresolved Failures
6. Verification State
7. Working Set
8. Next Actions
9. Historical References

所有 heading 必须存在。

优先级：

```text
Goal / Hard Constraints / Unresolved Failures / Verification / Working Set
> Decisions / Progress / Next Actions
> Historical References
```

不能最后简单 `summary[:limit]`；按 item 分配预算并 deterministic 截断。

顶部始终包含 freshness guard：

```text
Historical compacted context. Canonical Session/EventLog remain the audit source.
Repository files, git diff/status and test results may have changed; re-read/re-run when current truth matters.
```

## 16. Summary method

成功的 Hybrid summary：

```text
structured-hybrid-v1
```

Semantic call 失败后安全 fallback：

```text
structured-fallback-v1
```

C4 pruning-only 仍保持：

```text
summary_method="none"
```

## 17. 测试矩阵

至少覆盖：

1. 中文隐式约束无需关键词正则，由 FakeSemanticSummarizer 正确进入 Hard Constraints；
2. 英文/中文/混合语言 semantic fields 原样保留语言；
3. malformed semantic ToolCall -> safe fallback；
4. semantic model 试图写 Verification State 时不会覆盖 deterministic evidence；
5. successful file_write -> modified path；
6. old pytest fail + later pass -> PASS，旧 failure superseded；
7. latest test fail -> FAIL；
8. recent raw tail 不被 summary 改写；
9. canonical history 不变；
10. Stage A pruning 足够 -> 不调用 semantic summarizer；
11. Stage B 第一次触发 -> semantic summarizer 恰好调用一次；
12. active summary + small delta -> 不重复调用 summarizer；
13. active view 再次超过 threshold -> 重新 semantic compact；
14. 第二次 semantic compact 仍从 canonical history 构造，不吃 previous summary；
15. renderer tight budget 下 section heading 仍完整；
16. summary input budget 截断保持 message/unit 边界；
17. semantic summary usage 进入 turn-boundary SessionUsage；
18. round-boundary preflight summary usage 进入最终 RunResult / Chat usage；
19. `CONTEXT_COMPACTION_STARTED` 在 semantic LLM call 前产生；
20. Chat live UI 收到 started event 时输出 `[压缩上下文]`；
21. semantic failure 记录 failed event 且 Agent 仍可继续；
22. existing C4 tool pruning tests 全部不回归。

真实 provider 不参与单测；测试使用 FakeSemanticSummarizer / MockBackend。

## 18. 预计修改范围

生产代码：

1. 新增 `context/history_evidence.py`
   - 统一 Action / Observation / Params parser。

2. `context/tool_pruning.py`
   - 复用共享 parser；C4 行为不变。

3. 新增 `context/structured_compaction.py`
   - deterministic evidence；
   - semantic packet；
   - internal `record_context_summary` schema；
   - `LLMSemanticSummarizer`；
   - merge / fallback / bounded renderer。

4. `context/compaction.py`
   - Stage B Hybrid integration；
   - active compacted view reuse；
   - started/failed/success trace；
   - additional usage；
   - C4 Stage A 保持。

5. `agent/core.py`
   - `PrepareNextTurnResult.additional_usage`；
   - turn-boundary usage 聚合；
   - 不把 compaction 策略搬入 Core。

6. `agent/runner.py`
   - round-boundary preflight additional usage 聚合到最终 RunResult。

7. `agent/task.py`
   - 新增 `CONTEXT_COMPACTION_STARTED` / `CONTEXT_COMPACTION_FAILED` EventType。

8. `entry/chat.py`
   - started event 显示 `[压缩上下文]`；
   - 不展示 summary 内容。

9. `entry/cli.py`
   - 构造 Chat Context policy 时注入 `LLMSemanticSummarizer(backend)`；
   - 通用 event printer 可识别 started marker。

测试：

10. 新增 `tests/test_structured_compaction.py`
11. `tests/test_tool_pruning.py`
12. `tests/test_compaction.py`
13. `tests/test_chat.py`

明确不改：

- `agent/session.py`（checkpoint 仍是 `list[dict[str, Any]]`，无需 schema bump）
- `context/token_budget.py`
- `tools/*`
- `config/default.yaml`
- provider backend 实现（Anthropic/OpenAI-compatible/Responses 不增加新专用接口）

若实施证明 provider backend 必须修改，先停止并重新确认范围。

## 19. C5 验收标准

- 中文/英文自然语言约束不依赖关键词 regex；
- semantic summary 只在 Stage B 真正触发时调用，不是每轮调用；
- semantic call 通过现有 `LLMBackend` Tool Calling 抽象完成；
- Tool/verification/repository evidence 不由 semantic model 覆盖；
- `[压缩上下文]` 在 semantic call 开始前可见；
- summary LLM token/latency 有 Trace，usage 不漏记；
- active compacted view 避免每 step 重复 summary call；
- repeated re-compaction 从 canonical history 重建，不发生 summary-of-summary drift；
- semantic failure 有 safe fallback，不破坏 Session；
- recent raw tail 与 canonical history 均保持不变；
- C1~C4 回归全部通过后才标记 C5 完成。

## 20. C5 之后

C5 完成后先进入 B1 Context Policy 离线 benchmark，不直接实现 C6。

B1 要回答：

- Hybrid semantic summary 是否比 C4 deterministic pruning + legacy extractive 更能保留 early constraints；
- summary 额外 LLM token 是否值得；
- active view reuse 后每 solved task 的 provider input / summary-call 数量；
- 是否真的出现需要 `context_recall(event_ref)` 的稳定失败样本。

只有 B1/B2 证明需要旧 Tool 原文自主回查时，再实现 C6。
