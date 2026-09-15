# Forge Agent Context Compaction TODO

日期：2026-09-15

执行原则：一次只收口一个 Context 契约；每批生产代码修改前按 `AGENTS.md` 检查工作树/分支/remote/stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不为得到整齐数字重复运行已通过节点。

当前状态：C1、C2 已完成，并经用户本地 pytest 验证全部通过；C3 设计已重新收敛为 **Chat round-boundary preflight**，等待用户确认实施范围。

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

状态：设计收敛完成，等待用户确认实施范围。

### 目标

只解决两个基础契约，不把普通单次 Agent 首请求纳入 Compaction 生命周期：

1. Compaction checkpoint 当前只记录 HEAD，而 Chat Repo Map 刷新已经看 `HEAD + working tree`；
2. 对已有共享历史的 Chat 新 round（尤其 `/resume`）而言，该 round 的第一次 model call 发生在 `prepare_next_turn(step>1)` 之前，目前只能依赖 TokenBudget trim。

关键澄清：

- 全新单次 Agent task：不做 round-boundary preflight；
- 全新 Chat 第一轮：没有历史压力，不做 round-boundary preflight；
- Chat 第 2+ 轮 / resume 后新一轮：若存在既有历史，则在本轮第一次 LLM call 前检查 ContextPressure；低压时完全 no-op，高压时才生成一次性 compacted model view。

### 生命周期设计

- [ ] `RunRequest.round_boundary_preflight: bool = False`，默认关闭。
- [ ] `ExecutionRunner` 只把该显式标志传给 `Agent.run()`，不实现 Context 策略。
- [ ] `Agent.run(..., round_boundary_preflight=False)` 在初始化 history/token budget/repo map 后、进入 step loop 前执行可选 round-boundary context policy。
- [ ] 现有 in-run `step > 1 -> prepare_next_turn` 分支保持不变。
- [ ] round-boundary 与 turn-boundary 复用同一个 policy callback 和同一套 request parts，不新增第二套 compaction 算法。
- [ ] `PrepareNextTurnContext` 增加 boundary/phase 标记（例如 `"round" | "turn"`），便于 Trace 和策略区分来源。
- [ ] 每次 `Agent.run()` 开始时清空旧的一次性 history override，避免上一次中断残留到新 run。

### Chat 触发时机

- [ ] `ChatSession.run_round()` 在追加本轮 user message 之前记录 `had_prior_context`。
- [ ] 先将本轮 user message 追加到 canonical history，再启动 Runner。
- [ ] 只有 `had_prior_context=True` 时，把 `round_boundary_preflight=True` 传入 RunRequest。
- [ ] 因此压力估算和 compacted tail 必须包含最新 user message，但该消息只能出现一次。
- [ ] resume 与普通第 2+ 轮共用同一路径，不写两套逻辑。

### Repository fingerprint

- [ ] 新增共享 `context/repository_state.py`。
- [ ] 提供 `repository_fingerprint(repo_path)`，语义统一为 `HEAD + working-tree snapshot`。
- [ ] 复用现有 `agent.loop_detector.snapshot_repository()`，不复制 working-tree 扫描逻辑。
- [ ] `entry/chat.py` 删除本地 `_repository_revision()`，改用共享 helper。
- [ ] `context/compaction.py` checkpoint 改用同一 helper。
- [ ] Session 中现有字段名 `repo_revision` 保持兼容，但值语义升级为共享 fingerprint；不做 schema/version bump。

### Resume / checkpoint lineage

- [ ] canonical history 仍是恢复事实源；resume 后 model view 重新从 canonical history 计算，不恢复旧 summary 作为事实。
- [ ] `TraceableCompaction` 增加轻量 runtime lineage cursor，用于 continuation 的 `previous_checkpoint_id`。
- [ ] Chat `_restore_session()` 从已保存 `compaction_checkpoints` 的最后一项恢复 lineage cursor。
- [ ] `start_new_session()` 重置 active lineage，防止新 Session 连接到旧 Session checkpoint。
- [ ] `clear_history()` 重置 active lineage；历史 checkpoint 可继续作为审计记录保留，但后续新 checkpoint 不再把旧 context 当 previous active state。
- [ ] 不要求持久化/恢复旧 `summary_text`；当前 C2 设计始终从 canonical history 重新生成 model view。

### 失败与取消语义

- [ ] round-boundary preflight 使用与现有 prepare policy 一致的保守失败语义：callback 异常时本轮在任何 LLM/tool 调用前失败，不静默退回另一套未知 Context 行为。
- [ ] preflight 不修改 canonical history，因此失败/取消不能留下半写 summary。
- [ ] `CONTEXT_COMPACTED` Trace 增加 boundary/phase 字段；低压 no-op 不制造无意义 checkpoint。

### C3 预计修改范围（待确认）

- [ ] 新增 `context/repository_state.py`
  - 统一 repository fingerprint。

- [ ] `context/compaction.py`
  - checkpoint 使用共享 fingerprint；
  - 支持 boundary 字段；
  - 支持恢复/重置 checkpoint lineage；
  - 不改变 `extractive-v1`，不做 C4/C5。

- [ ] `agent/core.py`
  - `PrepareNextTurnContext` 增加 boundary；
  - `Agent.run()` 增加默认关闭的 `round_boundary_preflight`；
  - 抽取 round/turn 共用的 context-policy 调用薄层；
  - 保持现有 `step > 1` turn-boundary 调用契约。

- [ ] `agent/runner.py`
  - `RunRequest` 增加 `round_boundary_preflight=False`；
  - 只透传给 `Agent.run()`，不承载策略。
  - 这是为了避免 Chat 直接修改 Agent 私有 override 或依赖 Runner 是否重建 Agent 的隐藏实现细节。

- [ ] `entry/chat.py`
  - 使用共享 repository fingerprint；
  - 只有存在 prior shared history 的 round 才开启 preflight；
  - resume/new round 共用同一路径；
  - restore/new/clear 时维护 active checkpoint lineage。

- [ ] `tests/test_compaction.py`
  - dirty working-tree fingerprint；
  - round/turn boundary Trace；
  - canonical history 不变；
  - latest user message 不重复；
  - previous checkpoint lineage 恢复/重置。

- [ ] `tests/test_chat.py`
  - fresh first round 不 preflight；
  - round 2 首次 model call 可直接看到 compacted view；
  - resume 长 Session 首次 model call 可直接看到 compacted view；
  - Repo Map dirty worktree invalidation 继续通过。

### 明确不改

- [ ] `agent/session.py`：现有 schema 足够，C3 不做 version bump。
- [ ] `config/default.yaml`。
- [ ] structured summary、deterministic pruning、context recall、benchmark。
- [ ] 普通单次 Agent 默认行为。

### C3 验收

- [ ] HEAD 不变、working tree 变化时共享 fingerprint 变化。
- [ ] Compaction checkpoint 与 Chat Repo Map 使用一致 repo state 语义。
- [ ] fresh Chat 第一轮不触发 round-boundary preflight。
- [ ] Chat 第 2+ 轮在高 pressure 时，第一次 model call 直接使用 compacted view。
- [ ] resume 长 Session 在高 pressure 时，第一次 model call 直接使用 compacted view。
- [ ] low pressure round-boundary preflight no-op，不产生 checkpoint。
- [ ] latest user message 在 canonical history 与 model-visible view 中语义正确且不重复。
- [ ] canonical history 不被 preflight summary 污染。
- [ ] Agent.run 内现有 `step > 1 -> prepare_next_turn` turn-boundary 契约继续通过。
- [ ] resume 后 `previous_checkpoint_id` 能连接到保存的最后 checkpoint；new/clear session 后 lineage 重置。
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
