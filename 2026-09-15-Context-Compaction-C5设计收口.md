# Context Compaction C5 设计收口

日期：2026-09-15

## 1. 目标

C5 将 C4 Stage B 的 `extractive-v1` 自由文本截断替换为 **deterministic structured compaction baseline**。

C5 不引入额外 LLM summary call，不改变 canonical `ConversationHistory`，不引入 context recall，不把 summary 当作 Repository 当前事实源。

最终链路：

```text
canonical history
-> full request pressure
-> Stage A deterministic tool-output pruning
-> recompute pressure
   -> pressure below threshold: pruning-only model view
   -> still high:
      deterministic structured state
      -> render bounded structured summary
      -> recent raw tail
      -> next model request
```

C5 只替换 Stage B，不改 C1~C4 已验收契约。

## 2. 为什么不直接加 LLM summarizer

Pi 等 coding agent 已经使用固定结构的 compaction summary，例如 Goal、Constraints、Progress、Key Decisions、Next Steps、Critical Context，并保留 recent raw messages；同时还累计 read/modified files。

Forge 第一版不直接照搬“再调用一次 LLM 做 summary”，原因是：

1. 目前还没有离线 bad case 证明 deterministic baseline 不够；
2. 多一次 LLM 调用会引入成本、延迟、provider 差异和新的失败模式；
3. Forge 已经有稳定的 Action / Observation 文本格式、`event_ref`、canonical history 与 append-only EventLog，可以先从这些确定性证据构造 state；
4. Benchmark 需要先有可重复 baseline，之后才能判断 LLM semantic summary 是否真的带来收益。

与 Pi 的重要差异：Pi 可以迭代更新 previous summary；Forge C5 仍坚持从完整 canonical history 重新生成 structured state，避免 summary-of-summary drift。

## 3. StructuredContextState

新增独立 Context state，不把摘要重新写回 history。

第一版固定包含：

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
class StructuredContextState:
    goal: str
    hard_constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    completed: tuple[str, ...]
    in_progress: tuple[str, ...]
    blocked: tuple[str, ...]
    unresolved_failures: tuple[str, ...]
    verification_state: tuple[str, ...]
    read_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    next_actions: tuple[str, ...]
    historical_references: tuple[str, ...]
```

所有字段都由 deterministic extractor 产生；没有证据的字段写明确的 `None deterministically identified` / `Unknown`，不能靠猜补齐。

## 4. 证据来源与可信度

### 4.1 Goal

权威来源：当前 `PrepareNextTurnContext.task.description`。

Chat 每轮的 `Task.description` 就是本轮 user input，因此 Goal 不需要从历史文本猜。

### 4.2 Hard Constraints

只从 user-authored、非 Tool Observation 的历史消息中提取。

第一版采用保守 marker 规则识别显式约束，例如：

- English：`must`、`must not`、`do not`、`don't`、`never`、`only`、`without`、`avoid`、`preserve`、`keep`、`requirement`、`constraint` 等；
- 中文：`必须`、`不要`、`不得`、`不能`、`只`、`仅`、`避免`、`保留`、`不修改`、`不要改`、`不允许`、`要求` 等。

规则宁可略保守多保留，也不能为了“更像摘要”删掉早期 hard constraint。

去重保持首次出现顺序；不把 Tool Observation、Reflection 注入文本误当成用户约束。

### 4.3 Decisions

C5 不把 assistant `Thought:` 当作可靠事实决策源。

第一版只记录可以从已执行 Action 证明的高置信度决策，例如：

- `file_write(path=...)`：选择修改该文件；
- `git_add(...)` / `git_commit(...)`：选择暂存/提交；
- 其它无法确定为持久决策的 read/search/test Action 不进入 Decisions。

这样避免把模型的探索性 reasoning 误写成既定决策。

### 4.4 Progress / Completed

只从“Action 与随后 Observation 工具名一致 + SUCCESS”的已执行 unit 中提取高置信度完成项。

第一版主要记录：

- successful `file_write`；
- successful `git_add`；
- successful `git_commit`；
- round completion message 若在 old region 中存在，可作为历史完成证据，但不得覆盖 repo 当前事实。

### 4.5 Progress / In Progress

不从旧 history 猜当前正在做什么。

默认写：

```text
Continue current Goal; use recent raw context as the current execution state.
```

recent raw tail 才是当前 turn 的主要连续状态源。

### 4.6 Blocked 与 Unresolved Failures

ERROR Observation 不能简单全部写成 unresolved。

规则：

1. 先从 old summarized region 收集 failure；
2. 用 **完整 canonical / pruned full history（包括 recent tail）** 检查同一 Action fingerprint 是否后来成功；
3. 如果后来成功，旧 failure 标记为 superseded，不进入 `Unresolved Failures`；
4. 如果没有后续成功，保留 tool、关键 error 摘要、`event_ref`；
5. `Blocked` 直接引用尚未解决的 failure，不额外发明 blocker。

特殊验证工具 `test` / `pytest`：latest test observation 具有更高状态优先级，旧 test failure 在后续 test success 后视为 superseded。

## 5. Verification State

从完整 history 中找最新 `test` / `pytest` Observation；必要时也可识别明确执行 pytest 的 shell Action，但 V1 优先只处理专用 test tool，避免误判 shell。

结构：

```text
PASS / FAIL / UNKNOWN
historical detail
source event_ref
freshness warning
```

无论 PASS 还是 FAIL，都必须明确：

> This is historical verification evidence, not authoritative current repository state. Re-run tests if current state matters.

C5 不自动宣称最新 test 仍然有效，也不把 summary 变成测试真相源。

## 6. Working Set

从 Action `Params:` JSON 中确定性提取路径。

第一版：

Read / inspect paths：

- `file_read.path`
- `file_view.path`
- `git_diff.path`
- search/find 的显式 `path`（若存在）

Modified paths：

- successful `file_write.path`
- successful `git_add.path` / files 参数（若工具 schema 提供）

不从 shell command 自由文本猜修改文件；shell 修改无法稳定证明时不进入 modified set。

保持首次出现顺序并去重。

Working Set 渲染必须带 freshness guard：

```text
Historical working set only; re-read files/diff before relying on current contents.
```

## 7. Next Actions

C5 不生成“智能计划”。

第一版只给确定性安全 continuation：

1. Continue the current Goal.
2. Re-read current repository/test/diff state before relying on historical observations.
3. Resolve any listed unresolved failures before declaring completion.

若没有 unresolved failure，则第 3 条不输出。

## 8. Historical References

Summary 中保留关键 `event_ref`，用于审计定位；checkpoint 仍保存完整 `source_event_ids`。

Model-visible summary 不需要列出所有 event id，避免 refs 本身占满 Context。

优先保留：

1. unresolved failures；
2. latest verification；
3. completed write/commit evidence；
4. 其余 refs 只显示总数和 deterministic bounded sample。

注意：C6 未实现前，`event_ref` 仍只是审计引用，不能表述为 Agent 可自主 recall。

## 9. HistoryEvidence 共享解析层

C4 的 `context/tool_pruning.py` 已经有 Action/Observation regex parser。C5 不应再复制第三套消息 grammar。

建议新增：

`context/history_evidence.py`

提供：

- `ParsedAction`
- `ParsedObservation`
- `parse_action_message()`
- `parse_observation_message()`
- Action/Observation pair 校验
- `Params:` JSON 解析
- deterministic action fingerprint

然后：

- `context/tool_pruning.py` 改为复用该 parser；
- `context/structured_compaction.py` 也复用同一 parser。

这次 refactor 只统一解析契约，不改变 C4 pruning 规则。

## 10. Bounded Renderer

不能像旧 `_summary()` 一样最后直接 `[:limit]`，否则可能把结构截断在某个 section 中间。

C5 renderer 必须：

1. 固定 section 顺序；
2. 所有 section heading 必须存在；
3. 全局受 `max_summary_chars` 限制；
4. 优先级：Goal / Hard Constraints / Unresolved Failures / Verification / Working Set > Decisions / Progress > Historical References；
5. 单条 item 可 deterministic 截断，但不能把整个高优先级 section 静默裁掉；
6. 超出预算时写 `... additional items omitted deterministically`。

默认仍沿用现有 `max_summary_chars=2000`，不在 C5 顺手扩大 summary 预算。

## 11. Stage B 集成

C4 Stage A 保持不变。

若 pruning 后仍高 pressure：

```text
pruned full history
    |
    +-- old summarized region [0:tail_start)
    |
    +-- recent raw tail [tail_start:]
```

Structured extractor：

- summary 内容主要来自 old summarized region；
- supersession / Verification 判断允许观察完整 history；
- Goal 来自当前 task.description；
- renderer 生成 bounded Markdown；
- model-visible message 仍使用现有 synthetic compaction marker，保持 provider 兼容；
- recent raw tail 原样保留。

`summary_method` 更新为：

```text
structured-deterministic-v1
```

`summary_hash` 对最终 structured summary text 计算。

`source_event_ids` 仍来自原 canonical dropped region，不因为 structured extraction 丢失审计覆盖。

## 12. Freshness / Source-of-Truth 规则

Structured summary 顶部必须明确：

```text
Historical compacted context. Canonical Session/EventLog remain the audit source.
Repository files, git diff/status and test results may have changed; re-read/re-run when current truth matters.
```

因此：

- Repo 当前内容：repository/files 是权威源；
- Git 当前状态：重新 `git status/diff`；
- Tests 当前状态：重新运行 tests；
- Summary 只描述历史执行证据。

## 13. 失败与原子性

Structured extraction / rendering 全部是纯内存 deterministic 操作。

只有在以下内容全部成功生成后才 append `CompactionEntry` / checkpoint：

- structured state；
- rendered summary；
- summary hash；
- projected-after pressure；
- checkpoint payload。

如果 structured builder 失败：

- 不写半个 CompactionEntry；
- 不写半 checkpoint；
- canonical history 不变；
- 优先返回 Stage A pruning view（若有）；否则让现有 final TokenBudget trim 兜底；
- 不为了 summary 失败直接破坏整个 Session 可用性。

C5 不新增 provider 调用，因此无 summary API timeout/retry 问题。

## 14. 测试矩阵

新增 `tests/test_structured_compaction.py`，至少覆盖：

1. current `Task.description` -> Goal；
2. early English / Chinese hard constraint 保留；
3. Tool Observation / Reflection 不误判为 hard constraint；
4. successful file_write -> Decisions + Completed + modified path；
5. file_read/file_view -> read working set；
6. unresolved ERROR -> Blocked + Unresolved Failures + event_ref；
7. old failure 后同一 action success -> failure superseded；
8. old pytest failure 后 later test success -> Verification PASS，旧 failure 不 unresolved；
9. latest test failure -> Verification FAIL；
10. no test evidence -> Verification UNKNOWN；
11. renderer 在 tight limit 下所有 section header 仍存在；
12. deterministic 输入 -> byte-identical summary；
13. Historical References bounded，但 checkpoint `source_event_ids` 完整；
14. recent raw tail 不被 structured summary 改写；
15. canonical history 不变；
16. repeated compaction 仍从 canonical history 重建，不引用 previous summary；
17. C4 Stage A 足够时仍 pruning-only，不进入 structured Stage B；
18. C4 Stage A 不足时 Stage B 使用 `structured-deterministic-v1`；
19. 被 Stage A prune 的旧大 Tool 正文不会重新进入 structured summary。

现有 `tests/test_tool_pruning.py` 需要把 Stage B 断言从 `extractive-v1` 更新为 structured method，但 C4 Stage A 单测保持原样。

## 15. 预计修改范围

生产代码：

1. 新增 `context/history_evidence.py`
   - 统一 Action/Observation/Params parser 与 action fingerprint。

2. `context/tool_pruning.py`
   - 改为复用 history evidence parser；
   - 不改变 C4 pruning 行为。

3. 新增 `context/structured_compaction.py`
   - `StructuredContextState`；
   - deterministic extractor；
   - bounded renderer。

4. `context/compaction.py`
   - Stage B 从 `extractive-v1` 切换到 `structured-deterministic-v1`；
   - structured failure 的 availability fallback；
   - Stage A 与 checkpoint lineage 语义保持。

测试：

5. 新增 `tests/test_structured_compaction.py`

6. `tests/test_tool_pruning.py`
   - 只更新 Stage B 集成断言；
   - Stage A 契约保持。

明确不改：

- `agent/core.py`
- `agent/task.py`
- `agent/session.py`
- `agent/runner.py`
- `entry/chat.py`
- `entry/cli.py`
- `context/token_budget.py`
- `tools/*`
- `config/default.yaml`

如果实施中证明必须修改上述额外生产文件，先停止并重新确认范围。

## 16. C5 验收标准

- structured summary section 完整且 deterministic；
- early hard constraint 在 old region 被保留；
- unresolved failure 不因压缩消失，已被后续成功证明 superseded 的 failure 不再错误阻塞；
- latest test evidence 有明确 PASS/FAIL/UNKNOWN 与 freshness warning；
- read/modified working set 可追踪；
- recent raw tail 与 canonical history 均不被修改；
- Stage A 足够时仍不创建 structured summary；
- Stage B checkpoint `summary_method=structured-deterministic-v1`；
- repeated compaction 不出现 summary-of-summary；
- 不新增任何 LLM summary call；
- 定向 pytest 与受影响 Chat/Session 回归通过后，C5 才标记完成。

## 17. C5 之后

C5 完成后，Context Compaction 主实现链路已经具备：

```text
full request pressure
+ token-based recent raw tail
+ deterministic tool-output pruning
+ deterministic structured historical state
+ checkpoint lineage / resume
+ canonical history / EventLog audit source
```

下一步不应立即引入更复杂语义模块，而应先进入 B1 离线 Context Policy Benchmark。

C6 `context_recall(event_ref)` 只有在 B1/B2 出现稳定“structured compaction 后仍需要旧 Tool 原文细节”的失败样本时再做。
