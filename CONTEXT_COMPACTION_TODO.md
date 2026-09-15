# Forge Agent Context Compaction TODO

日期：2026-09-15

执行原则：一次只收口一个 Context 契约；每批生产代码修改前按 `AGENTS.md` 检查工作树/分支/remote/stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不把未执行测试写成通过。

当前状态：C1、C2、C3、C4、C5 已完成，并经用户本地 pytest 验证通过。Context Compaction 主实现已收口；下一阶段进入 B1 Context Policy 离线 Benchmark。C6 `context_recall(event_ref)` 继续数据驱动后置。

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

状态：已完成，用户本地 pytest 全部通过。

设计：`2026-09-15-Context-Compaction-C5设计收口.md`。
交接：`2026-09-15-Context-Compaction-C5改动内容.md`。

### 语义原则

- [x] 不用中英文关键词 regex 判断 Hard Constraints / Decisions。
- [x] 必须理解自然语言的字段只在 Stage B 真正触发时额外调用一次 LLM。
- [x] Tool / test / file / event_ref 等协议事实 deterministic 提取，LLM 无权覆盖。
- [x] 当前 Goal 直接使用 `task.description`。
- [x] semantic summary 通过内部-only `record_context_summary` Tool Schema 复用现有 `LLMBackend.complete()`，不新增 provider-specific completion API。
- [x] semantic packet 保留用户原语言，支持中文/英文/混合语言。
- [x] Forge 自动 `[REFLECTION]` prompt 不进入 user-authored semantic evidence。

### 压缩触发与前端反馈

- [x] Stage A pruning-only 保持静默。
- [x] Stage A 后仍高 pressure、准备发起 semantic summary LLM call 时写 `CONTEXT_COMPACTION_STARTED`。
- [x] Chat frontend 收到 started event 后显示 `[压缩上下文]`。
- [x] 成功后继续写现有 `CONTEXT_COMPACTED` checkpoint evidence。
- [x] semantic failure 写 `CONTEXT_COMPACTION_FAILED`，走 safe fallback，不让 memory maintenance 直接击穿 Agent task。

### Active Compacted View

- [x] semantic compaction 后缓存进程内 active compacted view。
- [x] 同一 Task 后续 turn 优先使用 `active summary + raw delta`，不每 step 重复调用 summary model。
- [x] active view 再次超过 threshold 才重新 semantic compact。
- [x] re-compaction 从 canonical history 构造 semantic packet，不吃 previous summary，避免 summary-of-summary drift。
- [x] Chat 新 round 的 `task.description` 变化时 active view 自动失效，避免陈旧 Goal。
- [x] Resume V1 不恢复 active summary；首次再次高压允许重新 compact。

### Structured State

固定 section：

- [x] Goal
- [x] Hard Constraints
- [x] Decisions
- [x] Progress / Completed / In Progress / Blocked
- [x] Unresolved Failures
- [x] Verification State
- [x] Working Set
- [x] Next Actions
- [x] Historical References

### Usage / Trace

- [x] semantic summary provider usage 计入 Chat round / Session usage。
- [x] policy 自己累计 side-call usage，由 `ChatSession` 统一 merge，不侵入 Agent Core/Runner。
- [x] round-boundary 与 turn-boundary 的 semantic side-call 都由同一 policy usage accumulator 覆盖。
- [x] checkpoint/Trace 记录 `summary_usage`、`semantic_duration_ms`、packet truncation、semantic error。

### Safe fallback / Compatibility

- [x] malformed ToolCall / provider failure 不改 canonical history。
- [x] semantic failure 使用 deterministic evidence + bounded user-authored raw excerpts，method=`structured-fallback-v1`。
- [x] fallback 不用 regex 假装理解中文约束。
- [x] 没有 semantic summarizer 的程序化/C1-C4 兼容路径继续使用 `extractive-v1`，避免小历史 token 反向膨胀。
- [x] final TokenBudget trim 继续做硬兜底。

### 实际生产范围

- [x] 新增 `context/history_evidence.py`
- [x] `context/tool_pruning.py`
- [x] 新增 `context/structured_compaction.py`
- [x] `context/compaction.py`
- [x] `agent/task.py`
- [x] `entry/chat.py`

设计阶段预计但最终无需修改：

- [x] `agent/core.py`：无需修改；usage 由 policy + Chat 合并。
- [x] `agent/runner.py`：无需修改；不把 compaction 策略搬入 Runner。
- [x] `entry/cli.py`：无需修改；C4 已完成真实 Chat policy wiring，C5 由 ChatSession `bind_backend()`。

测试：

- [x] 新增 `tests/test_structured_compaction.py`
- [x] `tests/test_tool_pruning.py`
- [x] `tests/test_compaction.py` 兼容回归。
- [x] 用户本地运行 `tests/test_structured_compaction.py tests/test_tool_pruning.py tests/test_compaction.py`：最终 33 tests 全部通过。
- [x] 用户本地运行 `tests/test_chat.py tests/test_session_store.py tests/test_day2.py`：全部通过。

明确未改：

- [x] `agent/session.py`
- [x] `context/token_budget.py`
- [x] `tools/*`
- [x] `config/default.yaml`
- [x] provider backend 实现。

## C6：Optional `context_recall(event_ref)`

状态：数据驱动后置。

- [ ] 只有 B1/B2 出现稳定“压缩后需要原始旧 Tool 细节”的失败样本才开始。
- [ ] 只读 Tool，复用 ToolRegistry/Trace/权限边界。
- [ ] 未实现前只表述“Trace 可审计回查”，不称 Agent 自主 recall。

## B1：Context Policy 离线 Benchmark

状态：下一阶段，待设计与实施范围确认。

目标：固定 prerecorded histories，不调用真实模型生成 Agent 历史；Hybrid variant 的 semantic summary call 成本单独记录。

Cases：

- [ ] `early-hard-constraint`
- [ ] `huge-tool-output`
- [ ] `action-observation-pair`
- [ ] `superseded-state`
- [ ] `repeated-compaction`
- [ ] `resume-long-session`
- [ ] `dirty-repo-revision`

指标：before/after tokens、compaction ratio、projected input、context pressure、hard constraints preserved、orphan units、source event coverage、recent tokens kept、repo state changed、checkpoint atomic、raw event traceable、summary-call count/usage、semantic duration。

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

正式 benchmark 前不得写具体 Token/成功率提升数字，不得宣称“长历史不丢约束”“Agent 可自主回查历史 Tool 结果”。当前可以表述“实现 Hybrid Structured Compaction”，但效果数字仍必须等 fixed commit + fixed cases + raw Trace + hidden verifier 支持后再形成 Claim。
