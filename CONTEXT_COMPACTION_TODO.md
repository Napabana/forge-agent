# Forge Agent Context Compaction TODO

日期：2026-09-15

执行原则：一次只收口一个 Context 契约；每批生产代码修改前按 `AGENTS.md` 检查工作树/分支/remote/stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不为得到整齐数字重复运行已通过节点。

当前状态：C1、C2 已完成，并经用户本地 pytest 验证全部通过；C3 已完成源码核对，等待用户确认修改范围。

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

## C3：统一 RepositoryState 与 Session Preflight

状态：源码核对完成，等待用户确认文件范围。

### 目标

解决两个剩余基础矛盾：

1. Compaction checkpoint 当前只记录 HEAD，而 Chat Repo Map 刷新已经看 HEAD + working tree；
2. `prepare_next_turn` 仍只在 `step > 1` 执行，恢复长 Session / 新 Chat round 的第一次 model call 没有 Compaction preflight。

### 设计任务

- [ ] 新增共享 repository fingerprint helper，统一语义为 `HEAD + working-tree snapshot`。
- [ ] Chat Repo Map invalidation 与 Compaction checkpoint 共用该 helper。
- [ ] 不改 Session state schema；现有 `repo_revision: str` 和 `compaction_checkpoints` 足以承载新语义。
- [ ] 增加独立 Chat/session preflight，发生在 Runner 进入 Agent step 1 之前。
- [ ] preflight 只构造/应用 model-visible context，不重复写用户消息。
- [ ] 保持现有 `prepare_next_turn` 生命周期不变：Agent.run 内仍只在完整 Tool turn 后、`step > 1` 调用。
- [ ] `/resume` 后第一次 model call 与普通新 round 第一次 model call 使用同一 preflight 语义。

### C3 预计修改范围（待确认）

- [ ] 新增 `context/repository_state.py`
  - [ ] 提供共享 `repository_revision(repo_path)`。
  - [ ] 复用现有 `agent.loop_detector.snapshot_repository()`，不复制 working-tree 扫描逻辑。

- [ ] `context/compaction.py`
  - [ ] checkpoint repo revision 改为共享 fingerprint。
  - [ ] 提供可被 Chat preflight 复用的同一 compaction/context-view 逻辑；不新增第二套策略。

- [ ] `agent/core.py`
  - [ ] 增加最薄的 preflight 接线/Context 构造入口，使 Chat 能在正式 `Agent.run()` 前生成一次性 model-view override。
  - [ ] 不改变 `Agent.run()` 内 `step > 1 -> prepare_next_turn` 的既有契约。

- [ ] `entry/chat.py`
  - [ ] 删除本地 `_repository_revision()` 重复实现，改用共享 helper。
  - [ ] 在每轮 user message 已进入 canonical history、正式 Runner 调用前执行 Context preflight。
  - [ ] resume 与普通 round 共用这一入口。

- [ ] `tests/test_compaction.py`
  - [ ] HEAD 不变但 working tree 改变时 fingerprint 改变。
  - [ ] checkpoint repo revision 与 Chat 使用同一 fingerprint。
  - [ ] step 1 preflight 可生成 compacted model view，canonical history 不变。
  - [ ] preflight 不重复用户消息。
  - [ ] `prepare_next_turn` step>1 集成测试继续通过。

- [ ] `tests/test_chat.py`
  - [ ] 普通新 round 第一次 model call 已经过 preflight。
  - [ ] resume 长 Session 后第一次 model call 已经过 preflight。
  - [ ] Repo Map dirty working-tree invalidation 行为继续通过。

### 明确不改

- [ ] `agent/session.py`：现有 schema 足够，C3 不做 version bump。
- [ ] `agent/runner.py`：优先不改；如果实际接线证明 Runner 必须新增字段/入口，立即停下重新确认范围。
- [ ] `config/default.yaml`。
- [ ] structured summary、deterministic pruning、context recall、benchmark。

### C3 验收

- [ ] HEAD 不变、working tree 变化时统一 repo fingerprint 变化。
- [ ] Compaction checkpoint 与 Chat Repo Map 使用一致 repo revision 语义。
- [ ] 普通 round 和 resume round 的第一次 model call 都可在 step 1 前完成 preflight。
- [ ] canonical history 不被 preflight summary 污染。
- [ ] user message 只追加一次。
- [ ] Agent.run 内 prepare_next_turn 的 step>1 契约不变。
- [ ] 定向 pytest 通过后再进入 C4。

## C4：Deterministic Tool-output Pruning

状态：未开始。

- [ ] 先处理旧 `file_read` 全文。
- [ ] 再处理旧 shell stdout/stderr。
- [ ] 再处理旧 pytest/test 长日志。
- [ ] 再处理重复 search/find 结果。
- [ ] 只有规则可证明时才处理已被后续状态覆盖的 Observation。
- [ ] Pruned Observation 必须保留 tool/status/error type/关键摘要/event_ref/`full_output_available=true`。
- [ ] hard constraint / user message 不被 Stage A pruning 修改。
- [ ] Action/Observation 仍保持 unit 完整。
- [ ] pruning deterministic。

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
