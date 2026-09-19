# P2 Agent Intelligence 执行计划

状态更新时间：2026-09-18  
当前实现基线：`dev@a9c745f77db4adae4818f381261a2dd4c151e552`

> 命名说明：仓库历史上已经存在 “P2 Repo Map” 实验与证据目录。本计划使用 **P2 Agent Intelligence** 作为新阶段名称，子任务编号为 P2-0 ～ P2-5，避免把旧 Repo Map P2 重新解释为未完成。

## 0. 阶段目标

P0/P1 已完成 Agent loop、Tool lifecycle、Permission/Hook/Cancel、Trace v2、Context Compaction、Session、Failure Harness、Repo Map、Acceptance、GitHub delivery 与 Evidence Pack。

P2 不再继续堆普通 Tool/UI，而是补齐 coding agent 的任务级智能闭环：

```text
Evaluation Harness
      ↓
Structured Planning
      ↓
Failure-aware Recovery / Replanning
      ↓
Agent Skills
      ↓
MCP Capability Integration
      ↓
Trajectory-driven Skill Evolution
```

核心原则：

1. 复用现有 `ExecutionRunner → Agent → ToolExecutor → Trace/RunResult`，不新增第二套 Agent loop。
2. 新能力必须能进入 Trace、可 deterministic 回归、可进入 Evaluation Harness 做 A/B。
3. 真实模型实验必须先冻结 case、模型、参数、重复数、输出目录；不覆盖历史结果。
4. P2 的“自进化”只允许生成候选并经过 Eval Gate；禁止生产运行时直接自改 prompt/skill 后自动生效。
5. MCP/Skill 必须服从现有 Permission、Hook、Cancel、Sandbox 与 Trace，不允许绕开 ToolExecutor。
6. 默认实现先做最小闭环；多 Agent、并行工具调用、LLM judge、大规模 SWE-bench 不作为首版目标。

---

# P2-0 Coding Agent Evaluation Harness

状态：**DONE**

实现摘要（2026-09-18）：

- 新增 `evals/coding_agent/`，形成 `EvaluationSuite → EvalTask → Trial → existing ExecutionRunner → deterministic Graders → TrialResult → EvalReport`。
- 正式 Agent 执行仍复用 `ExecutionRunner → Agent → ToolExecutor`；没有新增第二套 Agent loop，也没有修改 Trace v2 schema。
- 首版冻结 8 个小型 coding task，覆盖 single-file feature、bug fix、test regression、multi-file、repository navigation、completion/test requirement、recoverable test failure 与 final-state verification；每个 case 带 reference solution 自检。
- deterministic grader 首版支持 command/file/repository_state/run_trace，并从现有 RunResult/Trace 抽取 steps、tokens、wall time、tool/test/completion-rejection/reflection 等指标。
- fake/scripted backend 只作为 Harness correctness evidence，不输出 Agent capability pass rate。
- `baseline_react` 的真实模型实验本轮未执行；冻结 artifact 明确记录 `execution_status=not_executed`、`real_model_executed=false`。
- 输出目录默认拒绝覆盖；CLI 支持 suite / variant / repetitions / output_dir / task filter，并要求显式 `--real-model` 才会调用 Provider。
- 实现提交时 ChatGPT 环境未运行仓库级 pytest；用户随后在本地执行新增专项测试、相关 Runner/Failure Harness/Trace/Acceptance/Evidence Pack 回归以及全量 pytest，并明确确认全部通过。未提供 passed 数量或耗时，因此不补造数字。详见 `docs/changes/2026-09-18/P2-0-Coding-Agent-Evaluation-Harness.md` 与本地回归补充日志。

## 为什么先做

现有仓库已经有：

- `evals/context_policy_*.py`
- `evals/repo_map_*.py`
- `evals/verify_evidence_pack.py`
- Failure Harness
- AcceptanceContract
- Worktree / Docker isolate
- Trace v2

但它们主要是某个能力的专项 benchmark / regression，没有形成统一的“coding task → isolated trial → grader → trajectory/outcome → report”任务级 Evaluation Harness。

P2-0 先建立 baseline，后续 Planning / Recovery / Skills / Evolution 才能回答“是否真的变好”。

## 参考设计

### Anthropic Agent Evals

参考：Anthropic《Demystifying evals for AI agents》。

借用：

- Task / Trial / Grader / Transcript(Trajectory) / Outcome / Evaluation Harness 的概念拆分；
- 每个 trial 从干净隔离环境开始；
- coding agent 优先 deterministic grader；
- 同时记录 outcome 与 trajectory；
- 从小而明确的真实任务集开始。

不照搬：

- 首版不引入通用 LLM-as-judge；
- 不建设大规模并发评测平台。

### SWE-bench / SWE-agent

借用：

- repository task + isolated environment + patch/test grading；
- batch/eval 与交互式 agent harness 分离；
- trajectory 持久化用于后续分析。

不照搬：

- 首版不兼容完整 SWE-bench 数据格式；
- 不追求公开 benchmark 排名。

## 预期实现

建议新增：

```text
evals/coding_agent/
├── schema.py
├── runner.py
├── graders.py
├── report.py
└── cases/
```

Task Case 至少表达：

- id
- task description
- fixture/repository setup
- require_changes / require_tests
- hidden acceptance command 或 deterministic verifier
- tags（simple / multi-file / recovery / navigation / long-context 等）

Trial 输出至少记录：

- RunStatus / termination_reason
- acceptance
- steps
- total/provider tokens
- wall time
- tool call count
- test attempts
- completion rejection count
- trajectory/trace path
- patch/result artifact

第一批任务优先从当前人工 E2E 与真实失败中抽 8～12 个，不追求数量。

## 验收

- 默认离线 case 可在无 Provider credential 时运行；
- 每个 trial 是 clean worktree/fixture，不共享业务状态；
- grader 与 agent harness 分离；
- 生成 JSON/JSONL + 汇总 report；
- baseline 能冻结并进入 Evidence Pack；
- 能作为后续 feature A/B 的统一入口。

---

# P2-1 Structured Planning

状态：**DONE**  
依赖：P2-0 baseline

实现摘要（2026-09-19）：

- 新增 `agent/planning.py`，用 typed `ExecutionPlan / PlanStep / PlanRevision` 表达 runtime planning state；没有新增 Planner Agent 或第二套 loop。
- 复用既有 Function Calling `Action(TOOL_CALL)` 作为内部 `plan_create / plan_step_update / plan_revise` structured control surface；Provider parser 无需新增 PLAN Action。
- `planning_mode=off|auto|always` 进入正式 config 与 CLI/Chat/API/GitHub Issue；默认 `off` 保持 baseline。
- 当前 plan 作为 bounded runtime context 注入 system request，不重复写入 canonical ConversationHistory，因此 HistoryWindow/Compaction 不会把 current plan 当旧历史裁掉。
- Tool 增加保守 effect metadata：未知 Tool 默认 may-mutate；read-only 工具显式 opt-in。`always` 模式下 mutation 与 FINISH 前要求存在有效 plan。
- progress/revision 只接受模型显式 structured update，不从 Tool success 自动推断 semantic completion；未实现 P2-2 failure-aware automatic replanning。
- Trace v2 增加 plan lifecycle point events；P2-0 `TrialMetrics` 增加 planning metrics。
- Evaluation CLI 将 `baseline_react → off`、`planning → always`，共享 8-case outcome grader 不要求 baseline 产生 plan event。
- 用户随后在本地完成定向修复复跑与全量 `python -m pytest -q`，最终明确确认全部通过；最终通过轮次未提供具体 passed 数量或耗时，因此不补造数字。P2-1 状态已收口为 DONE。详见 `docs/changes/2026-09-19/P2-1-Structured-Planning.md` 与本地回归 DONE 日志。

## 当前缺口

`agent/prompt.py` 已有 Explore → Plan → Edit → Verify → Finish，但 Plan 只是 prompt-level instruction。

当前没有正式的：

- ExecutionPlan
- PlanStep
- step status
- plan revision
- replan reason
- plan lifecycle trace

因此模型可能“想过计划”，但 runtime 无法观测、约束、恢复或评测计划。

## 参考设计

### Claude Code Plan Mode

借用：

- Planning 阶段与 Mutation 阶段分离；
- 计划应明确文件、执行顺序、风险与验证方法；
- plan 本身是可审计 artifact；
- 实现偏离计划时允许更新 plan。

不照搬：

- Forge 首版不要求每个任务人工审批 plan；
- 不默认所有简单任务都进入 plan mode。

### Aider Architect Mode

借用：

- “方案/架构意图”和“具体文件编辑”职责分离；
- 复杂任务可先形成 solution proposal，再由执行阶段落实。

不照搬：

- 首版不强制使用两个不同模型，也不引入 Planner Agent + Editor Agent 多 Agent 架构。

## 预期实现

建议新增：

```text
agent/planning.py
```

核心数据：

- `ExecutionPlan`
- `PlanStep`
- `PlanStatus`
- `PlanRevision`

建议配置：

```text
planning_mode = off | auto | always
```

首版目标：

- simple task 可跳过结构化 planning；
- complex task 生成结构化 plan；
- plan 注入后续 model context；
- Agent 执行时更新 step 状态；
- Trace 记录 plan_created / plan_step_started / plan_step_completed / plan_revised；
- Context Compaction 必须保留当前 plan 的关键状态；
- plan 不是完成性权威，最终仍由 repository state + test + acceptance 判定。

## 验收

在 P2-0 固定任务集上至少比较：

```text
baseline ReAct
vs
Structured Planning + ReAct
```

观察：

- success/acceptance
- steps/tokens/time
- 无效探索
- completion rejection
- 复杂 multi-file task 的稳定性

不要求第一版证明总体成功率提升，只要求计划生命周期真实可观测、可消融。

---

# P2-2 Failure-aware Recovery + Replanning

状态：**DONE**  
依赖：P2-1

实现摘要（2026-09-19）：

- 新增 `agent/recovery.py`，用 typed `FailureContext / RecoveryDecision / RecoveryPolicy / RecoveryRuntime` 表达同一 Agent loop 内的 failure-aware recovery；没有新增 RetryAgent、Reviewer Agent 或第二套执行循环。
- 新增 `recovery_mode=off|structured` 与 `recovery_max_attempts`；默认 `off` 完全保留既有 Reflection / Completion Guard 行为，CLI / Chat / API / GitHub Issue 使用同一正式配置。
- structured recovery 覆盖 recoverable Tool/Test failure、Permission deny、Loop、No Progress 与 Completion rejection；策略为 `retry / inspect / rerun_test / change_approach / replan / give_up`。
- Provider transient retry 继续由既有 `_call_with_retry` 负责；cancel、ToolExecutor framework failure、prepare/context infrastructure failure 与 runtime fatal infrastructure 不包装成“智能恢复”。
- `REPLAN` 不是提示词标签：当已有 P2-1 plan 时，RecoveryRuntime 记录触发时的 plan version；在真实 `plan_revise` 产生更高版本之前，repository mutation 与 FINISH 被 gate，read-only diagnosis 仍允许。
- pending replan gate 作为 bounded runtime system context 每轮重新注入，因此 HistoryWindow / Context Compaction 不会通过裁剪旧 history 丢失当前 recovery requirement。
- Trace v2 新增 `failure_classified / recovery_selected / recovery_exhausted / recovery_blocked`；P2-0 TrialMetrics 增加 failure/recovery/replan/exhaustion metrics。
- Evaluation CLI 新增正式 architecture variant `planning_recovery`：`planning_mode=always + recovery_mode=structured`；既有 `baseline_react` 与 `planning` 语义保持不变。
- 新增 `tests/test_structured_recovery.py`，并扩展 Failure Harness、config 与 coding-agent eval regression；用户随后在本地完成专项、兼容、全量 pytest 与 Evidence Pack 校验并明确确认全部通过。最终通过轮次未提供具体 passed 数量或耗时，因此不补造数字；真实模型效果仍未执行。


## 当前缺口

Forge 已经有：

- provider retry
- Tool error Observation
- `reflection_test_failed`
- `reflection_no_edit`
- `reflection_loop_detected`
- Completion Guard recovery
- deterministic Failure Harness

但当前主要模式仍是：

```text
failure
→ 注入 reflection/rejection prompt
→ LLM 自己决定下一步
```

缺少结构化 Failure Context、Recovery Decision 与 Replan 关系。

## 参考设计

### SWE-agent

借用：

- `max_requeries` 对格式/blocked action/bash syntax 等错误进行局部恢复；
- RetryAgent 在 attempt 失败后 reset environment 并重新尝试；
- reviewer/retry loop 可决定是否 retry，并受 cost budget 限制；
- trajectory 保存每次 attempt。

不照搬：

- Forge 首版不做完整 multi-attempt 环境 reset；
- 不新增第二个 reviewer Agent 作为硬依赖。

### Aider lint/test loop

借用：

- 修改后自动执行 lint/test；
- 非零结果直接反馈给模型继续修复；
- recovery 围绕真实执行反馈，而不是抽象自省。

## 预期实现

建议新增：

```text
agent/recovery.py
```

核心模型：

```text
FailureContext
├── category
├── source
├── evidence
├── recent_actions
├── repo_state
├── test_state
└── plan_state

RecoveryDecision
├── strategy
├── reason
├── target_plan_step
└── budget_cost
```

建议 FailureCategory：

- TEST_FAILURE
- TOOL_FAILURE
- PERMISSION_DENIED
- LOOP
- NO_PROGRESS
- COMPLETION_REJECTED
- CONTEXT_FAILURE
- INFRASTRUCTURE

建议 RecoveryStrategy：

- RETRY
- INSPECT
- RERUN_TEST
- CHANGE_APPROACH
- REPLAN
- GIVE_UP

原则：

- deterministic 能判断的先 deterministic；
- infrastructure failure 继续遵守现有 fatal contract，不包装成“智能恢复”；
- recovery 有最大次数/预算；
- RecoveryPolicy 可触发 P2-1 plan revision；
- Trace 记录 failure_classified / recovery_selected / replan。

## 验收

P2-0 增加至少以下 case：

- test fail → root cause fix → retest
- completion rejected → recover
- repeated ineffective action → replan
- permission denied → change approach
- fatal infrastructure → 不错误 retry

比较 baseline reflection 与 structured recovery 的行为差异。

---

# P2-3 Agent Skills

状态：**DONE**  
依赖：P2-0；与 P2-1/P2-2 可组合

实现摘要（2026-09-19）：

- 新增 `skills/catalog.py` 与 `skills/runtime.py`，实现 filesystem Skill catalog、frontmatter 校验、project/global 分层、progressive disclosure runtime；没有新增 selector Agent 或第二套 Agent loop。
- 发现路径固定为 `<repo>/.agents/skills/` 与可配置 global root（默认 `~/.forge-agent/skills`）；project 同名 Skill 覆盖 global，同目录中的坏 Skill 被隔离为 `skill_rejected`，不会阻断整个 Agent。
- startup/system context 只常驻 name/description/source/resource counts；完整 `SKILL.md` 必须通过内部 `skill_load` 显式加载，reference 再通过 `skill_reference_load` 二次按需加载。
- loaded Skill / reference 保存在 per-run SkillRuntime 并每轮重新注入 system runtime context，不依赖 canonical ConversationHistory，因此 history trimming / compaction 不会丢当前 Skill state。
- scripts 仅暴露 manifest，Skill subsystem 没有任何 execute API；真正脚本/Shell/文件操作仍必须由模型显式调用现有 Tool，再走 ToolExecutor → Hook → Permission → Tool → Trace。
- Trace v2 新增 `skill_discovered / skill_selected / skill_loaded / skill_reference_loaded / skill_rejected`。
- 正式 config 新增 `skills_enabled`、global dir、loaded/context 上限；默认关闭，CLI / Chat / API / GitHub Issue 统一接线。
- P2-0 Eval 新增 `planning_recovery_skills` architecture variant，固定使用 `evals/fixtures/coding_agent/skills/` 的 bug-fix / test-and-verify / repository-navigation 三个 Skill，不依赖用户本地目录。
- Eval 新增 non-blocking `skill_selection` process grader 与 Skill lifecycle metrics；should-trigger / should-not-trigger 不改变 task success/acceptance。
- 新增 `tests/test_agent_skills.py`，并扩展 config / coding-agent eval regression；用户随后在本地完成 P2-3 专项、关键兼容、package discovery、not-executed Eval 接线、Evidence Pack 与全量 pytest，并明确确认全部通过。最终通过轮次未提供具体 passed 数量或耗时，因此不补造数字；real-model Skill effectiveness 仍未执行。


## 参考设计

### Anthropic Agent Skills

借用：

- Skill = filesystem directory；
- `SKILL.md` 包含 name / description / instructions；
- 可附带 scripts / references / templates；
- progressive disclosure：metadata 常驻，完整 instructions 相关时再加载，资源按需加载。

### OpenAI / Codex Skills

借用：

- Skill 用于可复用 workflow 与团队知识；
- 初始只暴露 name/description，完整 `SKILL.md` 选择后才进入 context；
- project-local skill 与全局 skill 分层；
- Skill 可与 MCP 工具组合，负责“什么时候、按什么顺序、如何处理结果”。

不照搬：

- 首版不实现 Skill marketplace；
- 不允许 Skill 自己绕过 Permission 直接执行脚本；
- scripts 只能通过现有 Tool/Shell lifecycle 执行。

## 预期实现

建议新增：

```text
skills/
├── loader.py
├── catalog.py
├── selector.py
└── ...
```

建议发现路径：

```text
<repo>/.agents/skills/
~/.forge-agent/skills/
```

Skill 最小结构：

```text
skill-name/
├── SKILL.md
├── references/   # optional
└── scripts/      # optional
```

首版能力：

1. 扫描并校验 SKILL.md frontmatter；
2. startup/context 只暴露 metadata；
3. 相关时加载完整 Skill；
4. references 按需读取；
5. scripts 不自动执行，只能走 ToolExecutor；
6. Trace 记录 skill_discovered / skill_selected / skill_loaded；
7. Skill context 纳入 TokenBudget / Context Compaction。

建议内置 2～3 个 coding skill 做证据：

- bug-fix
- test-and-verify
- pr-review 或 dependency-upgrade

## 验收

Eval Harness 增加：

- should-trigger
- should-not-trigger
- workflow adherence
- outcome
- token overhead

至少做：

```text
baseline
vs
skill enabled
```

并避免“所有任务都加载所有 Skill”。

---

# P2-4 MCP Client / Tool Adapter

状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**  
依赖：P2-0；在 P2-3 Skills 后实现

实现摘要（2026-09-19）：

- 使用官方 MCP Python SDK v2（当前依赖固定为 `mcp>=2.2.0,<3`），Forge 不手写 JSON-RPC，也没有新增顶层 `mcp/` 以避免 shadow 官方 package。
- 新增 `mcp_integration/`：`MCPClientManager` 在专用 event-loop thread 中长期持有官方 async `Client` context；同步 `MCPToolAdapter.execute()` 通过线程安全 future 调用，不改变 Agent/ToolExecutor 同步 public contract，也不按 ToolCall 重连。
- stdio 为必选主线；Streamable HTTP 因官方 v2 `Client(URL)` 可复用同一生命周期桥接，仅增加薄 transport 分支，本轮一并支持；旧 SSE 不作为主线。
- remote tools 统一命名为 `mcp__<server_id>__<remote_tool>`，deterministic sanitize/长度限制；任何 collision 由现有 ToolRegistry 显式拒绝，不允许覆盖 native Tool。
- remote `inputSchema` 必须是 JSON-serializable object schema；malformed schema 在注册阶段确定性拒绝；schema/description/tool-count 设硬上限，防止不可信 server 用无界 capability metadata 挤占 Host/context。
- Tool output 对 text/structured content 做确定性 bounded rendering；`is_error`、timeout、invalid args、remote transport/server/protocol failure、Forge manager lifecycle error 分别映射到现有 ToolResult/ToolErrorType 语义。
- 新增 `REMOTE_CAPABILITY` 错误类型；P2-2 会把它视为 recoverable TOOL_FAILURE，而不是 Forge infrastructure corruption。
- remote annotations 只作为 hint。server 默认 `trust_read_only_annotations=false`；只有显式信任该 server 且 tool 声明 `read_only_hint=true` 才映射 `READ_ONLY`，其余一律 `MAY_MUTATE_REPOSITORY` 并由 PermissionManager `CONFIRM`。
- MCP invocation 仍严格走 `LLM → ToolRegistry → ToolExecutor → Hook → Permission → MCPToolAdapter → MCPClientManager → official SDK`；Planning mutation gate、cooperative cancel 与 Trace v2 均复用现有路径。
- Runner direct/Chat 长会话由 ExecutionRunner 持有 manager；isolate/worktree 每次 run 独立 manager；CLI/API/GitHub/Eval 都显式 close，不依赖 `__del__`。
- Trace v2 复用既有 tool events，并增加 `mcp_server_capabilities / mcp_tool_discovered`；记录 server capability negotiation 与 local/remote tool correlation，不记录 URL/env/secret。
- P2-0 新增真实 architecture variant `planning_recovery_skills_mcp`，固定使用仓库内离线 stdio MCP fixture，不读取用户随机 MCP 配置；TrialMetrics 增加 MCP discovered/call/failure counts；另新增独立 `mcp_suite.json`，不改写原 8-case frozen suite，并要求 process trace 真正调用固定 MCP guidance tool。
- 新增 `tests/test_mcp_integration.py`、官方 SDK in-process protocol test、真实 local stdio Host E2E、server crash/timeout/multi-server/lifecycle/permission/hook/cancel/planning/recovery/package tests。
- 当前 GitHub/ChatGPT 环境未执行用户本地 pytest，因此不能写 DONE，也不能写 MCP real stdio E2E passed；等待用户本地回归后再补 Evidence Pack 的 regression claim。

## 参考设计

### Model Context Protocol 官方架构

借用：

```text
Host
 ├── Client ↔ Server A
 ├── Client ↔ Server B
 └── ...
```

以及：

- capability negotiation；
- Server 暴露 tools / resources / prompts；
- Host 负责权限、生命周期、用户授权与安全边界；
- 每个 server connection 保持隔离。

### OpenAI Skills + MCP

借用：

- MCP 提供实时信息/受控操作；
- Skill 提供可重复 workflow；
- “Capability” 与 “Workflow” 分离。

## Forge 中的位置

Forge 本身作为 MCP Host：

```text
Forge Agent
   ↓
MCP Client Manager
   ↓
MCP Server
   ↓
MCPToolAdapter
   ↓
现有 ToolRegistry
   ↓
ToolExecutor
   ├── Hook
   ├── Permission
   ├── Cancel
   └── Trace
```

关键要求：MCP Tool 不能直接从 Agent 绕开 ToolExecutor 调远端服务。

## 预期实现

优先使用官方/成熟 MCP Python SDK，不手写 JSON-RPC 协议。

建议新增：

```text
mcp_integration/
├── manager.py
├── adapter.py
└── registry.py
```

首版范围：

- server config；
- 至少 stdio transport；
- capability negotiation；
- list tools；
- schema → `LLMToolSchema` / Forge Tool Adapter；
- invoke tool；
- timeout/cancel/error mapping；
- permission classification；
- trace correlation；
- server lifecycle cleanup。

如 SDK 与当前依赖兼容且实现成本合理，再加 Streamable HTTP；否则明确延期，不为“支持两种 transport”破坏主线。

建议实际 coding-agent 场景：

- GitHub/Issue/CI 类 server；
- docs/knowledge server；
- observability/debug server。

本地 file/shell/test 保持 native Tool，不为了 MCP 而 MCP。

## 验收

必须有 fake/local MCP server 的 deterministic offline test：

- discover
- invoke success
- invalid schema
- timeout/failure
- permission deny
- cancel
- server disconnect

并有至少一个真实但低风险的 MCP E2E，再进入 Evidence Pack。

---

# P2-5 Trajectory-driven Skill Evolution

状态：**TODO**  
依赖：P2-0 + P2-3；可消费 P2-2 recovery trajectory

## 定位

不实现“Agent 在生产运行时自动改自己然后立刻生效”。

目标是：

```text
Trajectories
   ↓
Experience Mining
   ↓
Candidate Skill / Rule
   ↓
Evaluation Harness
   ↓
Promotion Gate
   ↓
Approved Skill Version
```

即 **eval-gated improvement**。

## 参考设计

### SWE-agent Demonstrations / Trajectories

借用：

- 完整 trajectory 作为可重放、可分析 artifact；
- 成功 trajectory 可转为 demonstration，指导未来任务；
- trajectory 与 config 一起保存，保证可复现。

### OpenAI Skill Evals

借用：

- skill 改动不能靠“感觉更好”；
- prompt → recorded trajectory/artifacts → deterministic checks → score；
- 同时看 outcome 与 process；
- should-trigger / should-not-trigger 都要测；
- 新 Skill/version 必须通过 eval 后再推广。

不照搬：

- 第一版不自动覆盖正式 Skill；
- 不做在线强化学习；
- 不训练模型参数；
- 不把一次成功 trajectory 直接当“经验真理”。

## 预期实现

建议新增：

```text
experience/
├── miner.py
├── candidate.py
├── promotion.py
└── store.py
```

Candidate 必须带 provenance：

- source trace ids
- source task ids
- failure/recovery pattern
- generated_at
- candidate version

流程：

1. 从成功 + acceptance pass 的 Trace 中提取重复 workflow；
2. 从失败 → recovery → success 的轨迹中提取可复用 recovery pattern；
3. 生成 **candidate** Skill，不进入正式 catalog；
4. 用 P2-0 跑 baseline vs candidate；
5. promotion gate 检查：
   - target case improvement / non-regression
   - should-not-trigger 无明显退化
   - token/step budget 不失控
   - deterministic safety checks pass
6. 只有通过 gate + 人工确认才进入正式 Skill 版本。

## 验收

至少完成一个真实闭环：

```text
historical traces
→ candidate skill
→ eval
→ reject 或 promote
→ provenance 可追溯
```

如果没有足够重复轨迹支撑“自动发现”，允许先用 fixture trajectory 验证机制；不得伪装成真实自进化收益。

---

# 1. 推荐执行顺序

严格按：

```text
P2-0 Evaluation Harness
        ↓
P2-1 Structured Planning
        ↓
P2-2 Failure-aware Recovery
        ↓
P2-3 Agent Skills
        ↓
P2-4 MCP
        ↓
P2-5 Trajectory-driven Evolution
```

原因：

- Eval first：后续每项都有 baseline 与 A/B；
- Planning before Recovery：Recovery 才能正式 replan；
- Skills before MCP：先建立 workflow/context 载体，再接外部 capability；
- Evolution last：必须建立在 trajectory + skills + eval gate 上。

---

# 2. P2 总体证据要求

每一项完成时都必须提供：

1. Implementation Fact：源码路径与关键接口；
2. Deterministic Regression：不依赖 Provider；
3. Eval Artifact：固定 case/variant/report；
4. 如有真实模型实验，单独标记 Real-model Small Sample；
5. docs/changes 对应变更记录；
6. Evidence Pack 新 claim 必须写限制条件；
7. 不修改历史 frozen result 来“提高指标”。

最终 P2 才允许形成类似：

```text
baseline ReAct
vs + Planning
vs + Recovery
vs + Skills
```

的统一 Agent architecture ablation。

---

# 3. 参考资料

- Anthropic, Demystifying evals for AI agents: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Claude Code Plan Mode / AI-native SDLC Playbook: https://academy.claude.com/courses/ai-native-sdlc-playbook/plan-mode
- Aider Chat Modes / Architect Mode: https://aider.chat/docs/usage/modes.html
- Aider Linting and Testing: https://aider.chat/docs/usage/lint-test.html
- SWE-agent agent/retry implementation: https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/agents.py
- SWE-agent Demonstrations: https://github.com/SWE-agent/SWE-agent/blob/main/docs/config/demonstrations.md
- MCP Architecture: https://modelcontextprotocol.io/specification/2025-03-26/architecture/index
- Anthropic Agent Skills: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- OpenAI Skills: https://developers.openai.com/zh-Hans/docs/build-skills
- OpenAI Skills + MCP: https://developers.openai.com/zh-Hans/plugins/concepts/skills
- OpenAI Skill Evals: https://developers.openai.com/zh-Hans/blog/eval-skills
