# P2-1 Structured Planning

日期：2026-09-19  
实现基线：`dev@60e1194d50f9d23c64a57f6e90d51f6842319bfd`  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 目标

P2-1 把原先 prompt-level 的 Explore → Plan → Edit → Verify 提升为同一 `Agent.run` 内真实、typed、可观测的 Planning runtime state，同时保持：

```text
ExecutionRunner
  ↓
Agent
  ↓
ToolExecutor
```

生产执行主链不变。Planning 不是第二个 Agent，也不替代 Completion Guard / Acceptance。

## 最终设计

### 1. Typed runtime state

新增 `agent/planning.py`：

- `PlanningMode = off | auto | always`
- `PlanningDecision`
- `PlanStepStatus = pending | in_progress | completed | skipped`
- `PlanStep`
- `ExecutionPlan`
- `PlanRevision`
- `PlanningRuntime`

Plan 不是自由文本字符串。首版对 goal、step 数量、step id、description、targets、verification 和 revision reason 做 bounded validation。

### 2. Structured control surface

没有新增 `PLAN` ActionType，也没有修改各 Provider parser。

复用当前所有 Provider 已统一支持的 Function Calling / `Action(TOOL_CALL)` 结构，向模型额外暴露三个 internal control schema：

```text
plan_create
plan_step_update
plan_revise
```

这些调用在 `Agent` 内被识别和消费，不进入 `ToolExecutor`，因为它们只是 runtime state 更新，不是 repository capability。

真正的 file/shell/test/git 工具仍严格走既有：

```text
ToolExecutor
→ validate
→ pre-hook
→ permission
→ tool
→ post-hook
→ Trace / Observation
```

因此 P2-1 没有复制 Tool lifecycle。

### 3. Planning mode

`off`：

- 默认值；
- 不增加 planning schema；
- 不注入 plan context；
- 不增加额外 planning model call；
- 保持现有 baseline ReAct 行为。

`always`：

- 允许先通过显式 read-only tool 做必要 exploration；
- repository mutation 前必须已有有效 `ExecutionPlan`；
- FINISH 前也必须已有有效 plan；
- plan step progress 只能通过显式 `plan_step_update` 更新，不把 Tool success 自动解释为 semantic completion。

`auto`：

首版使用 deterministic policy，不调用额外 LLM classifier。按稳定规则给出 decision reason：

- `require_tests=True` → plan；
- task description 中有至少两个明确 file path hint → plan；
- 出现 then / after / across / multiple / multi-file / several files 等 multi-step signal → plan；
- 否则判定 `auto_simple_task` 并 skip。

该规则只决定是否启用 Structured Planning，不判断 task 是否完成。

### 4. Mutation gate

为避免 Planning 自己维护 `WRITE_TOOLS = [...]`，在现有 Tool abstraction 增加最小 effect metadata：

```text
ToolEffect.READ_ONLY
ToolEffect.MAY_MUTATE_REPOSITORY
```

默认是 `MAY_MUTATE_REPOSITORY`，新/未知 Tool fail-safe。

首版显式标记 read-only：

- file_read / file_view
- search_text / find_files / find_symbol
- git_status / git_diff
- test

file_write、git_add、git_commit 与 shell 保持默认 may-mutate。shell 即使实际命令只是 `ls`，首版也不会在没有 plan 时放行，因为没有可靠的 command-level mutation metadata；这是有意的保守边界。

### 5. Plan context 与 Context Compaction

当前 plan 不重复 append 到 canonical `ConversationHistory`。

`Agent._render_request_parts` 每轮把 bounded current plan 渲染成独立 runtime context，并通过 `build_system_prompt(..., runtime_context=...)` 注入当前模型请求：

```text
canonical ConversationHistory
+ current ExecutionPlan
+ Repo Map
+ Tool schemas
→ provider request
```

因此：

- current plan 每轮可见；
- HistoryWindow / Context Compaction 不会把 plan 当旧 history 丢掉；
- canonical history 不保存完整 plan 副本；
- `prepare_next_turn` 的 pressure 计算看到同一个 system content；
- token breakdown 增加 diagnostic `planning_tokens`，但 provider-reported usage 仍是权威 usage。

P2-1 没有新增第二套 Planning memory。

### 6. Plan progress / revision

首版不做脆弱自动推断：

```text
file_write success ≠ PlanStep COMPLETED
```

模型必须显式调用：

```text
plan_step_update(step_id, status)
```

Revision 同样必须显式调用 `plan_revise`，并保存：

- previous_version
- new_version
- reason
- current full bounded plan

本轮没有实现 failure → RecoveryPolicy → replan；P2-2 后续可以消费 `current_plan` / current step，再调用同一 revision mechanism。

### 7. Trace v2

新增 point-in-time planning events：

- `plan_created`
- `plan_step_started`
- `plan_step_completed`
- `plan_revised`
- `planning_skipped`
- `plan_rejected`

仍是 Trace v2、同一 run correlation、同一最终写盘 redaction。没有 Trace v3。

Planning control 本身由正常 model call 产生，因此已有：

- `llm_call_started`
- `llm_call_finished`
- provider usage

自然覆盖 planning-related model tokens，不建立第二套 token accounting。

### 8. Completion authority 不变

没有修改现有 Completion Guard 的 repository/test freshness 判断。

即使所有 PlanStep 都是 COMPLETED，只要：

- required repository change 未发生；
- required tests 未运行；
- latest test failed；
- final state 未被最新成功测试覆盖；

FINISH 仍按原有 completion guard 拒绝。

Plan 只服务 execution state，不是 completion truth source。

## P2-0 Evaluation Harness 集成

正式 P2-1 architecture mapping：

```text
baseline_react → planning_mode=off
planning       → planning_mode=always
```

选择 `always` 作为首版 formal A/B，是为了保证共享 8-case suite 中 `planning` variant 真正代表一个不同 architecture，而不是由 `auto` 在简单任务上退化成 baseline。

共享 coding outcome graders 未增加 “必须有 plan event” 要求，因此 baseline 不会因为没有 plan 而失败。

`TrialMetrics` 从 Trace 新增派生：

- plan_created_count
- plan_revision_count
- plan_step_completed_count
- planning_skipped

real-model aggregate 也能记录这些 planning metrics；fake/scripted rows 仍只属于 Harness correctness evidence，不生成 capability pass rate。

## Config / 产品入口

正式配置：

```yaml
agent:
  planning_mode: off
```

`config/schema.py` 校验 `off | auto | always`，旧配置缺省为 `off`。

CLI / Chat / API / GitHub Issue 均把同一 config 值传入 `AgentConfig`，Planning 不是 eval-only feature。

## Deterministic regression

新增 `tests/test_structured_planning.py`，覆盖：

- schema validation / empty plan / duplicate step id；
- stable step ordering；
- off mode 不创建 plan、无额外 LLM call；
- auto simple skip / complex enable / deterministic reason；
- always mutation-before-plan gate；
- explicit step progress；
- revision version + lineage；
- current plan 注入后续 model context；
- canonical history 不重复保存完整 plan；
- prepare/history override 后 current plan 仍可见；
- normal provider usage accounting；
- malformed plan rejection 后可重新提交；
- Plan COMPLETED 不绕过 Completion Guard；
- Agent failure 后 plan Trace 保留；
- ToolEffect fail-safe；
- Eval variant baseline/planning 映射；
- TrialMetrics planning extraction；
- fake planning metrics 不冒充 real-model capability。

并扩展 `tests/test_coding_agent_eval.py` 的 metrics extraction fixture。

## 本轮实际验证

当前 ChatGPT 执行环境没有可执行的仓库 checkout，无法实际运行仓库 pytest。GitHub API 只能读取/提交 repository object，不能把本地用户工作区或 venv 作为执行环境。

因此本轮不声明任何 pytest passed 数量，也不把实现状态写成 DONE。

未执行：

- repository pytest；
- Docker E2E；
- real-model baseline/planning A/B。

## 本地回归补充（第一次，2026-09-19）

用户在本地执行相关 regression 集合，共收集 119 个测试：

- 118 passed
- 1 failed
- 失败：`tests/test_trace_v2.py::test_trace_v2_records_prepare_llm_tool_and_configured_hooks`

失败原因不是 Agent/Planning lifecycle 行为错误，而是 P2-1 新增 `planning_tokens` 后，Trace v2 的 exact-key 回归仍按旧 token breakdown 字段集合断言。

排查同时发现一个实际 accounting bug：`agent/core.py::_trace_token_breakdown` 已经从 `system_tokens` 中拆出 `planning_tokens`，但 `estimated_input_tokens` 求和时漏加该字段，启用 Planning 时会低估本地 diagnostic input estimate。

修复：

- `tests/test_trace_v2.py` 将 `planning_tokens` 纳入 Trace v2 token breakdown contract；
- baseline/default `planning_mode=off` 明确断言 `planning_tokens == 0`；
- `agent/core.py` 将 `planning_tokens` 纳入 `estimated_input_tokens`；
- `tests/test_structured_planning.py` 增加 Planning-on 时 `planning_tokens > 0` 与完整 token breakdown 恒等式回归。

该修复不改变 provider-reported usage、Completion Guard、Planning lifecycle 或 P2-0 grader outcome。修复提交后尚待用户本地复跑，因此 P2-1 状态仍为 **IMPLEMENTED / LOCAL VALIDATION PENDING**。

## 本地回归补充（第二次，2026-09-19）

用户拉取首轮 Trace accounting 修复后，执行：

```text
tests/test_trace_v2.py
tests/test_structured_planning.py
```

共收集 31 个测试：

- 30 passed
- 1 failed
- 失败仍为 `tests/test_trace_v2.py::test_trace_v2_records_prepare_llm_tool_and_configured_hooks`
- 断言：baseline/default `planning_mode=off` 期望 `planning_tokens == 0`，实际为 `1`

根因确认：`planning_mode=off` 时 `_planning_context_cache == ""`，但通用保守 `estimate_tokens("")` 返回 1。空 Planning context 不是一个真实可归因的 Planning token section，因此不能直接套用该保守估算结果。

修复：

- `agent/core.py::_trace_token_breakdown` 仅在 planning context 非空时调用 token estimator；
- 空 planning context 明确记录 `planning_tokens = 0`；
- `tests/test_structured_planning.py` 增加 off-mode empty-context 的专门回归；
- Planning-on 的 `planning_tokens > 0` 与完整 token breakdown 恒等式继续保留。

该修复只影响本地 diagnostic token attribution，不修改 provider-reported usage、Agent completion、Tool lifecycle 或 Evaluation outcome。修复后仍待用户本地复跑，因此 P2-1 状态继续保持 **IMPLEMENTED / LOCAL VALIDATION PENDING**。

## 本地回归补充（第三次，2026-09-19）

用户完成前两轮定向修复后执行全量 `python -m pytest -q`：

- collected 877
- 859 passed
- 14 skipped
- 4 failed
- 2 warnings

4 个失败分别位于 Chat、CLI isolate 和 config default 测试，但根因相同：`config/default.yaml` 中的裸值 `planning_mode: off` 被 PyYAML 按 YAML 1.1 规则解析为布尔值 `False`，随后字符串枚举校验拒绝 `"false"`。

修复：

- 默认配置改为显式字符串 `planning_mode: "off"`；
- 配置解析兼容 PyYAML 对裸 `off` 的 `False` 表示，并规范化回 `"off"`；
- 其他非字符串值仍拒绝，非法字符串仍拒绝；
- `tests/test_day6.py` 新增裸 YAML `off`、布尔 `False` 兼容和非法值回归。

这 4 个失败属于同一配置解析兼容性问题，不是 Chat、CLI、isolate 四条独立产品链路故障。修复后仍待用户本地复跑，因此 P2-1 状态继续保持 **IMPLEMENTED / LOCAL VALIDATION PENDING**。

## 本地验证命令

拉取本轮提交后：

```bash
python -m pytest -q tests/test_structured_planning.py tests/test_coding_agent_eval.py

python -m pytest -q \
  tests/test_agent_completion_guards.py \
  tests/test_runner.py \
  tests/test_trace_v2.py \
  tests/test_compaction.py \
  tests/test_structured_compaction.py \
  tests/test_token_budget_improvements.py \
  tests/test_coding_agent_eval.py \
  tests/test_failure_harness.py \
  tests/test_failure_harness_isolate.py \
  tests/test_evidence_pack.py

python -m evals.coding_agent \
  --variant planning \
  --output-dir evals/results/local-p2-1-planning-not-executed

python -m evals.verify_evidence_pack

python -m pytest -q
```

第三条命令没有 `--real-model`，因此只做 suite reference validation 并生成显式 `not_executed` artifact；输出目录必须事先不存在。

真实 A/B 后续由用户显式执行，例如分别用新的、当前不存在的 output directory 运行 `baseline_react` 和 `planning`，并显式添加 `--real-model`。本轮没有产生任何 success-rate / token / latency improvement claim。

## 明确未做

- FailureCategory / RecoveryPolicy / automatic failure → replan
- Planner Agent / Executor Agent / Multi-Agent
- Skills / MCP / Skill Evolution
- LLM-as-Judge
- Planning UI / approval
- SWE-bench compatibility
- 大规模付费 benchmark

下一阶段 P2-2 可以直接复用本轮的 `ExecutionPlan`、current step 和 `plan_revise` mechanism，而不改写 Agent 主循环。
