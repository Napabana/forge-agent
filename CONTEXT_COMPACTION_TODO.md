# Forge Agent Context Compaction TODO

日期：2026-09-15

执行原则：一次只收口一个 Context 契约；每批生产代码修改前按 `AGENTS.md` 检查工作树/分支/remote/stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不把未执行测试写成通过。

当前状态：C1、C2、C3、C4 已完成，并经用户本地 pytest 验证全部通过；C5 已重新设计为 **Hybrid Structured Compaction**，等待用户确认实施范围。

## 0. 实施门禁

- [x] 使用 `dev` 分支作为本轮实现分支。
- [x] 保留用户已有改动，不 reset/clean 覆盖工作树。
- [x] 不修改/还原 `config/default.yaml`。
- [x] 不同时引入 MCP、多 Agent、multi-tool call、向量检索、provider-native compact。
- [x] 每批先确认文件范围，再修改生产代码。
- [x] 每批记录真实测试结果。

## C1：Context 所有权与 HistoryUnit 收口

状态：已完成，用户本地 pytest 全部通过。

- [x] canonical `ConversationHistory` 不再按 message count 破坏性删除。
- [x] TokenBudget / Compaction 共用 HistoryUnit，Action/Observation 不拆。
- [x] recent tail 改为 token-based。
- [x] 交接：`2026-09-15-Context-Compaction-C1改动内容.md`。

## C2：完整 Request Pressure 与 CompactionEntry

状态：已完成，用户本地 pytest 全部通过。

- [x] 完整 request pressure：system/tool schema/history；Repo Map 仅诊断不重复计数。
- [x] summary 不写回 canonical history；`history_override` 只影响下一模型调用。
- [x] 独立 CompactionEntry / checkpoint / previous lineage。
- [x] 交接：`2026-09-15-Context-Compaction-C2改动内容.md`。

## C3：Repository fingerprint 与 Chat Round-Boundary Preflight

状态：已完成，用户本地 pytest 全部通过。

- [x] Repo fingerprint 统一为 HEAD + working-tree snapshot。
- [x] Fresh Round 1 不 preflight；Round 2+ / resume 首次模型调用前检查 Context policy。
- [x] resume/new/clear checkpoint lineage 正确恢复/重置。
- [x] 真实 `agent chat` production wiring 已在 C4 前置补齐。
- [x] 交接：`2026-09-15-Context-Compaction-C3改动内容.md`。

## C4：Deterministic Tool-output Pruning

状态：已完成，用户本地两组 pytest 全部通过。

- [x] `DeterministicToolPruner` 只改 model-visible copy。
- [x] ERROR/TIMEOUT、无 event_ref、recent raw tail、file_view 不 prune。
- [x] file_read / shell / test 成功大输出 deterministic pruning。
- [x] search/find exact duplicate 只保留最新结果。
- [x] Stage A 后重新计算 pressure；足够则 pruning-only，否则进入 Stage B。
- [x] canonical history、Action/Observation unit、event_ref 保持。
- [x] CLI Chat 注入会话级 `TraceableCompaction`。
- [x] 用户本地运行：`tests/test_tool_pruning.py tests/test_compaction.py` 全部通过。
- [x] 用户本地运行：`tests/test_chat.py tests/test_session_store.py tests/test_day2.py` 全部通过。
- [x] 交接：`2026-09-15-Context-Compaction-C4改动内容.md`。

## C5：Hybrid Structured Compaction

状态：设计已重新收口，等待用户确认实施范围。

详细设计：`2026-09-15-Context-Compaction-C5设计收口.md`。

### 语义原则

- [ ] 不再用中英文关键词 regex 判断 Hard Constraints / Decisions。
- [ ] 必须理解自然语言的字段只在 Stage B 真正触发时额外调用一次 LLM。
- [ ] Tool / test / file / event_ref 等协议事实 deterministic 提取，LLM 无权覆盖。
- [ ] 当前 Goal 直接使用 `task.description`。
- [ ] semantic summary 通过内部-only `record_context_summary` Tool Schema 复用现有 `LLMBackend.complete()`，不新增 provider-specific completion API。
- [ ] semantic packet 保留用户原语言，支持中文/英文/混合语言。

### 压缩触发与前端反馈

- [ ] Stage A pruning-only 保持静默。
- [ ] Stage A 后仍高 pressure、准备发起 semantic summary LLM call 时写 `CONTEXT_COMPACTION_STARTED`。
- [ ] Chat/CLI frontend 收到 started event 后显示精确提示：`[压缩上下文]`。
- [ ] 成功后继续写现有 `CONTEXT_COMPACTED` checkpoint evidence。
- [ ] semantic failure 写 `CONTEXT_COMPACTION_FAILED`，走 safe fallback，不让 memory maintenance 直接击穿 Agent task。

### Active Compacted View

- [ ] semantic compaction 后缓存进程内 active compacted view。
- [ ] 后续 turn 优先使用 `active summary + raw delta`，不每 step 重复调用 summary model。
- [ ] active view 再次超过 threshold 才重新 semantic compact。
- [ ] re-compaction 从 canonical history 构造 semantic packet，不吃 previous summary，避免 summary-of-summary drift。
- [ ] Resume V1 可不恢复 active summary；首次再次高压允许重新 compact。

### Structured State

固定 section：

- [ ] Goal
- [ ] Hard Constraints
- [ ] Decisions
- [ ] Progress / Completed / In Progress / Blocked
- [ ] Unresolved Failures
- [ ] Verification State
- [ ] Working Set
- [ ] Next Actions
- [ ] Historical References

### Usage / Trace

- [ ] semantic summary provider usage 必须计入 SessionUsage / RunResult / Chat usage。
- [ ] `PrepareNextTurnResult` 增加 additional usage 载体。
- [ ] turn-boundary 与 round-boundary preflight 都不能漏记 summary call token。
- [ ] checkpoint/Trace 记录 summary usage，benchmark 可分离 normal Agent call 与 compaction call 成本。

### Safe fallback

- [ ] malformed ToolCall / provider failure 不写半 checkpoint。
- [ ] 优先复用 active view；否则 deterministic evidence + bounded user-authored raw excerpts。
- [ ] fallback 不用 regex 假装理解中文约束。
- [ ] final TokenBudget trim 继续做硬兜底。

### C5 预计生产范围（待确认）

- [ ] 新增 `context/history_evidence.py`
- [ ] `context/tool_pruning.py`
- [ ] 新增 `context/structured_compaction.py`
- [ ] `context/compaction.py`
- [ ] `agent/core.py`
- [ ] `agent/runner.py`
- [ ] `agent/task.py`
- [ ] `entry/chat.py`
- [ ] `entry/cli.py`

测试：

- [ ] 新增 `tests/test_structured_compaction.py`
- [ ] `tests/test_tool_pruning.py`
- [ ] `tests/test_compaction.py`
- [ ] `tests/test_chat.py`

明确不改：

- [ ] `agent/session.py`
- [ ] `context/token_budget.py`
- [ ] `tools/*`
- [ ] `config/default.yaml`
- [ ] provider backend 实现；若证明必须改，先重新确认。

## C6：Optional `context_recall(event_ref)`

状态：数据驱动后置。

- [ ] 只有 B1/B2 出现稳定“压缩后需要原始旧 Tool 细节”的失败样本才开始。
- [ ] 只读 Tool，复用 ToolRegistry/Trace/权限边界。
- [ ] 未实现前只表述“Trace 可审计回查”，不称 Agent 自主 recall。

## B1：Context Policy 离线 Benchmark

C5 收口后开始。固定 prerecorded histories，不调用真实模型生成 Agent 历史；Hybrid variant 的 summary call 真实成本需要单独记录。

Cases：

- [ ] `early-hard-constraint`
- [ ] `huge-tool-output`
- [ ] `action-observation-pair`
- [ ] `superseded-state`
- [ ] `repeated-compaction`
- [ ] `resume-long-session`
- [ ] `dirty-repo-revision`

指标：before/after tokens、compaction ratio、projected input、context pressure、hard constraints preserved、orphan units、source event coverage、recent tokens kept、repo state changed、checkpoint atomic、raw event traceable、summary-call count/usage。

## B2：真实 Agent 消融

Variants：

- [ ] A `budget_trim_only`
- [ ] B `deterministic_pruning`
- [ ] C `hybrid_compaction`
- [ ] `legacy_window_40` 只作辅助对照。

Pilot：5 cases × 3 variants × 1 repeat = 15 runs。

Formal：5 cases × 3 variants × 3 repeats = 45 runs，冻结 Forge commit/model/provider/prompt/tool schema/max_steps/context budget/verifier。

主指标：Hidden verifier pass rate、Provider input tokens per solved task。

辅助指标：false-finish、compaction ratio、p50/p95 input tokens、summary calls、summary tokens、max pressure、tool calls、latency、cached/input tokens、checkpoint count。

## Claim 门禁

正式 benchmark 前不得写具体 Token/成功率提升数字，不得宣称“长历史不丢约束”“Agent 可自主回查历史 Tool 结果”“支持语义压缩”。只有 fixed commit + fixed cases + raw Trace + hidden verifier 支持后，才能形成简历 Claim。
