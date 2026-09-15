# Forge Agent Context Compaction TODO

日期：2026-09-15

执行原则：一次只收口一个 Context 契约；每批生产代码修改前按 `AGENTS.md` 检查工作树/分支/remote/stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不为得到整齐数字重复运行已通过节点。

当前状态：C1、C2、C3 已完成并经用户本地 pytest 验证全部通过；C4 代码、测试与 review 修复已完成，等待用户本地 pytest 验收。

## 0. 实施门禁

- [x] 使用 `dev` 分支作为本轮实现分支。
- [x] 保留用户已有改动，不 reset/clean 覆盖工作树。
- [x] 不修改/还原 `config/default.yaml`。
- [x] 不同时引入 MCP、多 Agent、multi-tool call、向量检索、provider-native compact。
- [x] 每批先确认文件范围，再修改生产代码。
- [x] 每批记录真实测试结果，不把未执行测试写成通过。

## C1：Context 所有权与 HistoryUnit 收口

状态：已完成，用户本地 pytest 全部通过。

- [x] `ConversationHistory` 不再按 message count 在 Compaction 前破坏性删除 canonical history。
- [x] TokenBudget 与 Compaction 共用统一 `HistoryUnit`。
- [x] Action/Observation 不可拆。
- [x] recent tail 从固定消息数改为 `keep_recent_tokens`。
- [x] checkpoint 记录 token-based tail 证据。
- [x] `tests/test_compaction.py` 及受影响回归通过。
- [x] 交接：`2026-09-15-Context-Compaction-C1改动内容.md`。

## C2：完整 Request Pressure 与 CompactionEntry

状态：已完成，用户本地 pytest 全部通过。

- [x] 定义 `ContextPressure`：total/reserve/available/system/tool schema/repo map/history/projected input/pressure ratio。
- [x] Repo Map 已嵌入 system prompt，仅作为诊断字段，不重复计入 projected total。
- [x] 新增 `history_limit_for_request()`，最终 model-visible history 受完整 fixed request 占用约束。
- [x] 定义独立 `CompactionEntry`。
- [x] summary 不再永久写回 canonical `ConversationHistory`。
- [x] `PrepareNextTurnResult.history_override` 只影响下一次模型调用。
- [x] repeated compaction 使用 `previous_checkpoint_id`，避免 summary-of-summary。
- [x] checkpoint 记录 projected input / after / available / pressure。
- [x] Core 仅提供 request parts 与消费一次性 override，不承载 Compaction 策略。
- [x] 用户本地运行：`tests/test_compaction.py` 全部通过。
- [x] 用户本地运行：`tests/test_chat.py tests/test_session_store.py tests/test_day2.py` 全部通过。
- [x] 交接：`2026-09-15-Context-Compaction-C2改动内容.md`。

## C3：统一 RepositoryState 与 Chat Round-Boundary Preflight

状态：已完成，用户本地 pytest 全部通过。

实际实现比最初设计更收敛：没有修改 `agent/core.py`，而是在 Runner 的共享 history 执行边界做 continuation preflight。

- [x] 新增 `context/repository_state.py::repository_fingerprint()`，统一 `HEAD + working-tree snapshot`。
- [x] Chat Repo Map invalidation 与 Compaction checkpoint 共用同一 repository fingerprint。
- [x] Fresh 单次 Agent / Fresh Chat Round 1 不做 round-boundary preflight。
- [x] 已有 shared history 的 Chat Round 2+ / resume，在第一次 LLM call 前复用同一个 Context policy。
- [x] pressure 低时 no-op；pressure 高时可直接生成一次性 `history_override`。
- [x] canonical history 不被 preflight summary 污染，当前 user message 不重复。
- [x] Agent 内 `step > 1 -> prepare_next_turn` turn-boundary 契约保持不变。
- [x] `TraceableCompaction` 支持 restore/reset checkpoint lineage。
- [x] resume 后下一 checkpoint 能连接 persisted 最后 checkpoint；new/clear session 后 lineage 重置。
- [x] `agent/session.py` schema 不需要 version bump。
- [x] 用户本地运行：`tests/test_compaction.py` 全部通过。
- [x] 用户本地运行：`tests/test_chat.py tests/test_session_store.py tests/test_day2.py` 全部通过。
- [x] 交接：`2026-09-15-Context-Compaction-C3改动内容.md`。

补充：C3 验收后源码审查发现真实 `agent chat` CLI 尚未注入 `TraceableCompaction`；该 production wiring 已作为 C4 前置补齐，并由 `tests/test_chat.py` 增加防回归断言。

## C4：Deterministic Tool-output Pruning

状态：代码、测试与 review 修复已完成，等待用户本地 pytest 验收。

### Stage A 规则

- [x] 新增 `context/tool_pruning.py::DeterministicToolPruner`。
- [x] 只修改 model-visible copy，不修改 canonical history。
- [x] 只 prune SUCCESS Observation；ERROR/TIMEOUT 保持原样。
- [x] 没有 `event_ref` 的 Observation 不 prune，确保可审计回查。
- [x] 同时校验 assistant `Action: <tool>` 与 Observation header tool 一致。
- [x] recent raw tail 仍按原 canonical history 的 token-based HistoryUnit 先固定，Stage A 不改变 recent boundary。
- [x] 旧大 `file_read`：保留 `File: ...` 元信息与审计 marker，移除大正文。
- [x] 旧大 `shell` SUCCESS：保留 deterministic head/tail。
- [x] 旧大 `test` / `pytest` SUCCESS：保留尾部摘要。
- [x] `search_text` / `find_files` / `find_symbol`：只 prune exact duplicate interaction，保留最新一份。
- [x] 三次以上 exact duplicate 时所有旧副本都直接指向最终保留的最新 `event_ref`，不形成 pruned-ref 链。
- [x] `file_view` 不 prune。
- [x] hard constraint / 普通 user message 不被 Stage A 修改。
- [x] Action/Observation 数量和顺序保持，pruned Observation 保留 role / tool_call_id / event_ref。
- [x] marker 使用 `full_output_available=true`，仅表示 canonical Observation 的完整 output 可从 EventLog 审计回查。
- [x] pruning deterministic；`pruned_event_ids` 按 canonical history 顺序输出。

### Stage A -> Stage B 组合

- [x] full request pressure 达到 threshold 后才进入 Stage A。
- [x] Stage A 后重新计算 request pressure。
- [x] 若已降到 threshold 以下，返回 pruning-only `history_override`，不制造 extractive summary。
- [x] pruning-only checkpoint 使用 `summary_method="none"`，同时记录 pruning token 证据。
- [x] 若 Stage A 后仍高压，Stage B `extractive-v1` 基于已经 pruning 的旧 Tool view 生成 summary。
- [x] Stage B `source_event_ids` 仍从原 canonical dropped region 收集。
- [x] pruning-only checkpoint 也更新 lineage cursor，下一 checkpoint 能正确连接。

### C3 production wiring 补齐

- [x] `entry/cli.py::chat()` 创建会话级 `TraceableCompaction()`。
- [x] 同一 policy 实例传入 `ChatSession(..., prepare_next_turn=context_policy)`。
- [x] Round 2+、resume lineage 与 C4 pruning 共用同一 runtime policy state。
- [x] 普通 `agent run` 默认行为不因此改变。

### 测试与验收

- [x] 新增 `tests/test_tool_pruning.py`，覆盖 file_read/shell/test/search pruning、recent tail、ERROR 保留、determinism、pruning-only 与 Stage B fallback。
- [x] `tests/test_chat.py` 增加真实 CLI 注入 `TraceableCompaction` 的防回归断言。
- [ ] 用户本地运行：`tests/test_tool_pruning.py tests/test_compaction.py`。
- [ ] 用户本地运行：`tests/test_chat.py tests/test_session_store.py tests/test_day2.py`。
- [ ] 两组通过后标记 C4 完成并进入 C5。
- [x] 交接：`2026-09-15-Context-Compaction-C4改动内容.md`。

## C5：Structured Compaction

状态：未开始。

第一版结构必须保留：Goal、Hard Constraints、Decisions、Progress(Completed/In Progress/Blocked)、Unresolved Failures、Verification State、Working Set、Next Actions、Historical References。

边界：

- [ ] Repo 当前事实不得由 summary 成为权威源。
- [ ] 最新 test/diff/file state 需要时重新读取。
- [ ] 明确 stale/superseded 状态处理。
- [ ] 先做 deterministic structured extractor baseline。
- [ ] 没有离线 bad case 证明必要性前，不引入额外 LLM summary call。
- [ ] summary/checkpoint 失败时保持旧 Context 可用，不写半 checkpoint。

## C6：Optional `context_recall(event_ref)`

状态：数据驱动后置。

- [ ] 只有 benchmark 出现稳定“压缩后需要原始旧 Tool 细节”的失败样本才开始。
- [ ] 只读 Tool。
- [ ] 复用 ToolRegistry/ToolExecutor/Trace/输出截断。
- [ ] 不绕开 Session/仓库权限边界。
- [ ] 调用进入 Trace。
- [ ] 未实现前只表述“Trace 可审计回查”，不称 Agent 自主 recall。

## B1：Context Policy 离线 Benchmark

C1~C5 收口后开始。固定 prerecorded histories，不调用真实模型生成历史。

Cases：

- [ ] `early-hard-constraint`
- [ ] `huge-tool-output`
- [ ] `action-observation-pair`
- [ ] `superseded-state`
- [ ] `repeated-compaction`
- [ ] `resume-long-session`
- [ ] `dirty-repo-revision`

指标：before/after tokens、compaction ratio、projected input、context pressure、hard constraints preserved、orphan units、source event coverage、recent tokens kept、repo state changed、checkpoint atomic、raw event traceable。

证据：固定 runner revision + manifest/hash + `raw.jsonl` + `report.json` + `report.md`；正式 benchmark dirty working tree 默认拒绝或显式记录 allow-dirty。

## B2：真实 Agent 消融

Variants：

- [ ] A `budget_trim_only`
- [ ] B `deterministic_pruning`
- [ ] C `hybrid_compaction`
- [ ] `legacy_window_40` 只作为辅助 legacy 对照。

Cases：early hard constraint、long Tool output/root cause、superseded test state、cross-file working set、double compaction + save/resume。

Pilot：5 cases × 3 variants × 1 repeat = 15 runs，只检查 fixture/trigger/Trace/verifier/指标管线。

Formal：5 cases × 3 variants × 3 repeats = 45 runs，冻结 Forge commit/model/provider/prompt/tool schema/max_steps/context budget/prerecorded history/verifier。

主指标：Hidden verifier pass rate、Provider input tokens per solved task。

辅助指标：false-finish、compaction ratio、p50/p95 input tokens、max pressure、tool calls、p50/p95 latency、cached/input tokens、cache-write tokens、checkpoint count、context recall calls（若实现）。

## Claim 门禁

正式 benchmark 前不得写具体 Token/成功率提升数字，不得宣称“长历史不丢约束”“Agent 可自主回查历史 Tool 结果”“支持语义压缩”。只有 fixed commit + fixed cases + raw Trace + hidden verifier 支持后，才能形成简历 Claim。
