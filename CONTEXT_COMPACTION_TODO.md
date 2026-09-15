# Forge Agent Context Compaction TODO

日期：2026-09-15

执行基线：以实际开始实现时 `dev` 最新 commit 为准；规划形成时分析基线为 `a86c4e48f3cd4235866371d8f534dc346e93c905`。

执行原则：一次只收口一个 Context 契约；每批生产代码修改前先按 `AGENTS.md` 检查 `git status --short --branch`、最近提交、remote、stash，列出拟修改文件和理由，等待用户确认。测试先定向后扩展，不为得到整齐数字重复运行已通过节点。

当前状态：C1 已完成并经用户本地 pytest 验证全部通过；C2 尚未开始，等待用户确认修改范围。

## 0. 实施前门禁

- [x] 本地 pull 最新 `dev`，确认已包含 `CONTEXT_COMPACTION_EXECUTION_PLAN.md` 与本 TODO。
- [x] `git status --short --branch`，确认并保留用户已有修改。
- [x] `git log -3 --oneline --decorate`。
- [x] `git remote -v`，不得修改私有 SSH remote 约定。
- [x] `git stash list`，不得丢弃已有 stash。
- [x] 阅读本地 `AGENTS.md` 最新“最后交接”。
- [x] 不修改/还原 `config/default.yaml`。
- [x] 记录正式实现基线 commit。
- [x] 运行当前 Compaction 最小基线测试，只记录真实结果，不为失败外问题扩大修改范围。
- [x] 确认不同时引入 MCP、多 Agent、multi-tool call、向量检索、provider-native compact。

## C1：Context 所有权与 HistoryUnit 收口

状态：已完成，用户本地 pytest 全部通过。

### 目标

让 Canonical History 不再在 Compaction 之前被 message-count window 无条件永久删除，并让 History/Compaction/TokenBudget 共用一致的 Tool turn 单元语义。

### 已完成修改

- [x] `context/history.py`
  - [x] 明确 canonical history 与 bounded/model-view history 的职责。
  - [x] Compaction/Chat 使用场景不再提前 destructive trim。
  - [x] 保持旧构造方式兼容。
  - [x] 不在该文件加入摘要策略。

- [x] `context/token_budget.py`
  - [x] 将 `_HistoryUnit` 及 conversation-unit 构造逻辑提取为 Context 可复用的 `HistoryUnit`/helper。
  - [x] 保持 `trim_history()` 原行为和现有测试兼容。
  - [x] 保证 assistant Action + user/tool Observation 始终不可拆。

- [x] `context/compaction.py`
  - [x] 改为复用统一 HistoryUnit。
  - [x] 将 `retained_tail` 消息数语义替换为 `keep_recent_tokens`。
  - [x] 从最近 unit 向前累计 token，完整保留 unit。
  - [x] 本批保留 `extractive-v1` 作为 baseline，不引入 LLM summary。

- [x] `tests/test_compaction.py`
  - [x] 长 History 在进入 Compaction 前没有被 message window 偷先删除。
  - [x] Action/Observation 不产生 orphan。
  - [x] token-based recent tail 在 Observation 长度悬殊时仍满足预算语义。
  - [x] 现有 Compaction/Session 相关回归保持通过。

### C1 验收

- [x] Canonical History 与 model-visible trim 的职责在代码注释/测试中可解释。
- [x] Compaction 不再按固定消息数理解 recent tail。
- [x] Tool turn unit 只有一套定义。
- [x] 用户本地运行 Compaction 相关 pytest，确认全部通过。
- [x] 已新建 `2026-09-15-Context-Compaction-C1改动内容.md` 并记录真实结果。
- [x] 汇报 diff 和真实测试结果。

## C2：完整 Request Pressure 与 CompactionEntry

### 目标

让触发条件反映真实下一次模型请求压力，并将 Compaction 状态从真实用户 History 中分离。

### 设计任务

- [ ] 定义 `ContextPressure` 或等价稳定结构：
  - [ ] total/context budget；
  - [ ] reserve；
  - [ ] system estimate；
  - [ ] tool schema estimate；
  - [ ] repo map estimate；
  - [ ] context/history estimate；
  - [ ] projected input；
  - [ ] pressure ratio。

- [ ] 优先复用现有 `TokenBudget`，不新增 tokenizer/provider 依赖。
- [ ] 定义独立 `CompactionEntry`：
  - [ ] checkpoint id；
  - [ ] summary method/hash；
  - [ ] summary/structured state；
  - [ ] source event IDs / source unit range；
  - [ ] before/after tokens；
  - [ ] repo state hash；
  - [ ] keep recent tokens；
  - [ ] previous checkpoint id。
- [ ] `CompactionEntry` 不作为原始 `role=user` 永久写回 canonical history。
- [ ] 由 Context View 组装阶段将 compaction state 渲染成 model-visible message。
- [ ] repeated compaction 明确引用 previous checkpoint，而不是无限 summary-of-summary。

### 拟修改范围（实施前重新确认）

- [ ] `context/token_budget.py`
- [ ] `context/compaction.py`
- [ ] `agent/core.py`（只允许薄接线；禁止把策略塞回主循环）
- [ ] 必要时新增 `context/manager.py`
- [ ] `tests/test_compaction.py`

### C2 测试

- [ ] History 较小但 Repo Map/tool schema 大时可正确触发 pressure。
- [ ] History 大但完整请求仍低压时不无意义提前 compact。
- [ ] CompactionEntry 与真实 user message 可区分。
- [ ] 连续两次 compact 可定位两次来源关系。
- [ ] `_build_messages()` 的最终 estimated input 不超过目标 available budget（允许 estimator 误差边界需明确）。

## C3：统一 RepositoryState 与 Session Preflight

### 目标

解决 HEAD 未变但 working tree 已变化，以及 resume/new round step 1 不执行 compaction 的问题。

### 任务

- [ ] 抽取或复用统一 `RepositoryState`/fingerprint helper。
- [ ] repo state 至少包含 `HEAD + working-tree snapshot`。
- [ ] Repo Map、Compaction checkpoint、Session 后续都引用同一语义。
- [ ] `/resume` 后新 user round 在 Runner 前进行 Context preflight。
- [ ] 普通新 Chat round 同样可在 step 1 前检查 pressure。
- [ ] 保持 `prepare_next_turn` 的既有语义：第一 Agent step 不调用；只在完整 Tool turn 后调用。

### 拟修改范围（实施前重新确认）

- [ ] repo state helper 所在文件（先检查现有实现，避免重复模块）。
- [ ] `context/compaction.py` / ContextManager。
- [ ] `entry/chat.py`。
- [ ] `agent/session.py` / session store tests（仅必要字段）。
- [ ] `tests/test_compaction.py`。
- [ ] Session 相关直接测试。

### C3 测试

- [ ] HEAD 不变、working tree 改动后 repo state hash 改变。
- [ ] 恢复长 Session 后第一次真实 model call 前已经完成 preflight。
- [ ] preflight 不重复插入用户消息。
- [ ] prepare_next_turn step>1 生命周期测试继续通过。
- [ ] checkpoint 保存/恢复后 compaction state 可重建。

## C4：Deterministic Tool-output Pruning

### 目标

在调用任何摘要模型之前，先移除/缩写低长期价值且可从 Trace 回查的大块 Tool 输出。

### 支持顺序

- [ ] 旧 `file_read` 全文。
- [ ] 旧 shell stdout/stderr。
- [ ] 旧 pytest/test 长日志。
- [ ] 重复 search/find 结果。
- [ ] 已被后续验证状态覆盖的 Observation（只在规则可证明时）。

### Pruned Observation 必须保留

- [ ] tool name；
- [ ] success/error status；
- [ ] stable error type；
- [ ] 最小关键摘要；
- [ ] event_ref；
- [ ] `full_output_available=true` 或等价标记。

### 测试

- [ ] 10k-token Tool 输出可显著降 token。
- [ ] hard constraint/user message 不被 Stage A pruning 修改。
- [ ] source event IDs 完整。
- [ ] Action/Observation 仍保持 unit 完整。
- [ ] pruning 结果 deterministic。

## C5：Structured Compaction

### 目标

仅当 C4 后仍超过 pressure 时压缩语义状态。

### 第一版结构（必须）

- [ ] Goal
- [ ] Hard Constraints
- [ ] Decisions
- [ ] Progress: Completed / In Progress / Blocked
- [ ] Unresolved Failures
- [ ] Verification State
- [ ] Working Set
- [ ] Next Actions
- [ ] Historical References

### 重要边界

- [ ] Repo 当前事实不得由 summary 成为权威源。
- [ ] 最新 test/diff/file state 需要时重新读取。
- [ ] 明确 stale/superseded 状态处理。
- [ ] 先做 deterministic structured extractor baseline。
- [ ] 没有离线 bad case 证明必要性前，不引入额外 LLM summary call。

### 测试

- [ ] 非首条 early hard constraint 保留。
- [ ] 早期已解决错误不被列为 current unresolved。
- [ ] 最新 verification state 胜过旧状态。
- [ ] second compaction 不无限重复旧 summary 文本。
- [ ] summary/checkpoint 失败时保持旧 Context 可用，不写半 checkpoint。

## C6：Optional `context_recall(event_ref)`（数据驱动后置）

- [ ] 只有 benchmark 出现稳定“压缩后需要原始旧 Tool 细节”的失败样本才开始。
- [ ] 只读 Tool。
- [ ] 复用现有 ToolRegistry/ToolExecutor/Trace/输出截断。
- [ ] 不允许绕开 Session/仓库权限边界。
- [ ] 调用本身进入 Trace。
- [ ] 统计 recall calls per solved task。
- [ ] 未实现前文档只表述“Trace 可审计回查”，不称 Agent 自主 recall。

## B1：Context Policy 离线 Benchmark（C1~C5 收口后）

### Fixture

固定 prerecorded histories，不调用真实模型生成历史。

- [ ] `early-hard-constraint`
- [ ] `huge-tool-output`
- [ ] `action-observation-pair`
- [ ] `superseded-state`
- [ ] `repeated-compaction`
- [ ] `resume-long-session`
- [ ] `dirty-repo-revision`

### 指标

- [ ] before_tokens
- [ ] after_tokens
- [ ] compaction_ratio
- [ ] projected_input_tokens
- [ ] context_pressure
- [ ] hard_constraints_preserved
- [ ] orphan_units
- [ ] source_event_coverage
- [ ] recent_tokens_kept
- [ ] repo_state_hash_changed
- [ ] checkpoint_atomic
- [ ] raw_event_traceable

### 证据输出

- [ ] 固定 runner revision。
- [ ] manifest/hash。
- [ ] `raw.jsonl`。
- [ ] `report.json`。
- [ ] `report.md`。
- [ ] dirty working tree 默认拒绝正式 benchmark，或显式记录 allow-dirty。

## B2：真实 Agent 消融

### Variants

- [ ] A `budget_trim_only`
- [ ] B `deterministic_pruning`
- [ ] C `hybrid_compaction`
- [ ] `legacy_window_40` 只做 legacy 辅助对照，不作为正式主 baseline。

### Cases

- [ ] early hard constraint + bug fix + API signature verifier。
- [ ] long Tool output + root-cause task。
- [ ] superseded test state。
- [ ] cross-file working set。
- [ ] double compaction + save/resume。

### Pilot

- [ ] 5 cases × 3 variants × 1 repeat = 15 runs。
- [ ] 只检查 fixture、trigger、Trace、verifier、指标管线。
- [ ] pilot 数字不得写入简历收益 Claim。

### Formal run

- [ ] 5 cases × 3 variants × 3 repeats = 45 runs。
- [ ] 冻结 Forge commit。
- [ ] 冻结 model/provider/protocol。
- [ ] 冻结 prompt/tool schema。
- [ ] 冻结 temperature/随机性设置（provider 支持时）。
- [ ] 冻结 max_steps/context budget。
- [ ] 冻结 prerecorded history/verifier。
- [ ] 保存每个 run 原始 Trace、patch、verifier result。

### 主指标

- [ ] Hidden verifier pass rate。
- [ ] Provider input tokens per solved task。

### 辅助指标

- [ ] false-finish rate。
- [ ] compaction ratio。
- [ ] p50/p95 input tokens。
- [ ] max context pressure。
- [ ] tool calls。
- [ ] p50/p95 latency。
- [ ] cached_tokens / input_tokens。
- [ ] cache_write_tokens。
- [ ] checkpoint count。
- [ ] context recall calls（仅 C6 实现后）。

## Claim 门禁

正式 benchmark 前不得写：

- [ ] “Context Compaction 降低 Token X%”。
- [ ] “任务成功率提升 X%”。
- [ ] “长历史不丢约束”。
- [ ] “Agent 可自主回查历史 Tool 结果”。
- [ ] “支持语义压缩”。

只有固定 commit + fixed cases + raw Trace + hidden verifier 支持后，才能将对应条目转成简历 Claim。
