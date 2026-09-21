# Forge Agent

Forge Agent 是一个面向软件工程任务的本地 Coding Agent。项目的核心不是单独实现一个 ReAct 循环，而是把“模型决策、仓库上下文、受控工具执行、隔离工作区、任务状态、可恢复会话、独立验收和可审计交付”组织成一条可解释、可测试的工程链路。

当前 `dev` 的主线架构已经收口：CLI `run`、交互式 `chat`、HTTP API 与 GitHub Issue 入口共享 `ExecutionRunner → Agent → ToolExecutor → EventLog/Trace` 的生产路径；隔离任务在此基础上接入 SQLite WAL TaskEngine、Git Worktree 和可选 Docker Runtime；Chat 额外接入持久化 Session 与两阶段 Context Compaction；GitHub Issue 自动 PR 则在 Agent 结束后增加独立 Acceptance 与确定性交付门禁。

> 使用方法见 [`USAGE.md`](USAGE.md)。项目实现、测试和 benchmark 的证据边界见 [`docs/evidence/README.md`](docs/evidence/README.md)。

P2 Agent Intelligence 已完成 Structured Planning、Failure-aware Recovery、Agent Skills、MCP capability integration、统一 Coding Agent Evaluation Harness，以及 post-run 的 trajectory-driven Skill Evolution。它们均复用现有执行/Trace/权限边界；真实效果是否提升必须通过单独 real-model evaluation 证明。

## 1. 整体架构

Forge Agent 将系统拆成六个彼此独立但可组合的层：入口层、执行控制层、上下文层、工具安全层、隔离与状态层、审计与交付层。

```text
                         ┌─────────────────────────────────────────────┐
                         │                Product Entrypoints          │
                         │ CLI run | Chat | HTTP API | GitHub Issue   │
                         └──────────────────────┬──────────────────────┘
                                                │
                                                ▼
                                      ┌───────────────────┐
                                      │  ExecutionRunner  │
                                      │ agent/runner.py   │
                                      │ request / accept  │
                                      │ trace / isolate   │
                                      └─────────┬─────────┘
                                                │
                         direct ────────────────┤────────────── isolate
                                                │                    │
                                                │                    ▼
                                                │          ┌───────────────────┐
                                                │          │ orchestrate_run   │
                                                │          │ TaskEngine (WAL)  │
                                                │          │ WorktreeSession   │
                                                │          │ Docker(optional)  │
                                                │          └─────────┬─────────┘
                                                │                    │
                                                └─────────┬──────────┘
                                                          ▼
                                                ┌───────────────────┐
                                                │     Agent.run     │
                                                │ agent/core.py     │
                                                │ sync ReAct loop   │
                                                └─────┬─────┬───────┘
                                                      │     │
                           ┌──────────────────────────┘     └──────────────────────┐
                           ▼                                                       ▼
                ┌──────────────────────┐                              ┌──────────────────────┐
                │      Context Plane   │                              │   Tool Safety Plane  │
                │ Model-aware Budget   │                              │ validate → pre-hook  │
                │ Persistent Repo Map  │                              │ → permission → tool  │
                │ History / Compaction │                              │ → post-hook          │
                └──────────┬───────────┘                              └──────────┬───────────┘
                           │                                                       │
                           └──────────────────────────┬────────────────────────────┘
                                                      ▼
                                             ┌───────────────────┐
                                             │ EventLog / Trace  │
                                             │ JSONL Trace v2    │
                                             │ RunResult         │
                                             └─────────┬─────────┘
                                                       │
                         ┌─────────────────────────────┴────────────────────────────┐
                         ▼                                                          ▼
              Chat Session checkpoint                                  GitHub Acceptance / Delivery
              state.json + round logs                                  verifier → commit → push → PR
```

这个拆分解决的是 Coding Agent 中几个容易互相污染的问题：

- 模型怎么“思考”和工具怎么“安全执行”不混在一起；
- Agent 主循环保持同步、易测试，异步资源生命周期放到组合根处理；
- canonical history、给模型看的压缩视图、运行审计日志和 Session 恢复状态分别管理；
- Worktree 负责 Git 工作区隔离，Docker 负责进程/网络/资源隔离，两者不混为一个概念；
- Agent 自己声称“完成”和独立验收结果分离，自动 PR 只有在两层条件都满足后才发生。

## 2. 四个入口如何复用同一执行内核

入口代码位于 `entry/`，但真正的产品执行组合根是 `agent/runner.py::ExecutionRunner`。

| 入口 | 入口文件 | 运行形态 | 额外能力 |
| --- | --- | --- | --- |
| `agent run` | `entry/cli.py` | direct 或 isolate | `--confirm`、`--sandbox`、`--isolate`、成果策略 |
| `agent chat` | `entry/cli.py` + `entry/chat.py` | direct | 多轮共享 History、Session 持久化、Context Compaction |
| HTTP API | `entry/api.py` | isolate | 后台 worker、SSE、cooperative cancel、任务查询 |
| GitHub Issue | `entry/github_issue.py` | direct 工作分支 | Issue → Agent → Acceptance → commit/push/PR |

`ExecutionRunner.run(RunRequest)` 负责统一以下语义：

1. 从 Task 和 `AcceptanceContract` 生成本次运行契约；
2. 解析 `entrypoint`，把 CLI / Chat / API / GitHub Issue 标识传入 Trace；
3. direct 模式创建/复用 `Agent` 与 `ToolExecutor`；
4. isolate 模式切换到 `orchestrate_run()`；
5. Agent 结束后执行独立 acceptance；
6. 统一补写 `acceptance`、`run_terminated` 等 post-run Trace；
7. 返回结构化 `RunResult`，而不是让每个入口自行解释失败状态。

因此入口主要处理用户 I/O、配置与产品行为，Agent 生命周期本身没有四套实现。

## 3. 执行控制：同步 ReAct 内核

核心文件：

- `agent/core.py`：`Agent`、`AgentConfig`、`prepare_next_turn` 生命周期；
- `agent/task.py`：`Task`、`Action`、`Observation`、`RunResult`、状态枚举；
- `agent/loop_detector.py`：重复动作和无进展检测；
- `agent/prompt.py`：System / Task prompt 与完成、反思提示。

`Agent.run()` 是同步控制循环，主链可以概括为：

```text
Task
 ↓
构建/复用 ConversationHistory
 ↓
Model-aware TokenBudget + Repo Map
 ↓
for step in 1..max_steps
  ├─ cancel boundary
  ├─ prepare_next_turn（step > 1；shared-history 首轮由 Runner 对齐同一语义）
  ├─ build_messages(system + repo map + history)
  ├─ LLMBackend.complete/stream
  ├─ parse → Action
  ├─ TOOL_CALL → ToolExecutor → Observation
  ├─ Observation 写入 History / EventLog
  ├─ Reflection / LoopDetector / completion guard
  └─ FINISH / GIVE_UP / FAILED / CANCELED / INCOMPLETE
 ↓
RunResult
```

### 3.1 为什么内核保持同步

模型调用、动作解析、工具调用、Observation、Reflection 和终止判断本质上构成严格有序的状态转换。当前实现让 `Agent.run()` 保持同步，使单步行为、失败语义和离线 failure injection 更容易确定性测试。

真正需要异步生命周期的部分——TaskEngine、Worktree、AgentBus——集中在 `agent/orchestrate.py`。这避免为了资源编排把整个 Agent loop 改成 async 状态机。

### 3.2 完成不是只看模型输出

Agent 的 `FINISH` 会经过完成性守卫。Task 会根据描述推断 `require_changes` / `require_tests`，Core 会跟踪：

- 是否真正完成过文件写入；
- 仓库状态是否发生变化；
- 是否执行过要求的测试以及最近测试状态；
- 是否存在未解决的 fatal runtime/infrastructure 错误。

模型可以提出 FINISH，但不满足完成条件时会被拒绝并继续运行。这是“模型声明完成”与“运行时确认完成”的第一层分离。

### 3.3 终止状态

`RunResult` 不把所有失败压成一个布尔值。当前主状态包括 `SUCCESS`、`FAILED`、`CANCELED`、`INCOMPLETE`、`GAVE_UP`，并通过 `termination_reason` 区分 `provider_error`、`infrastructure_error`、`canceled`、`loop_detected`、`resource_exhausted` 等原因。

其中取消是 cooperative cancellation：已经进入同步 Provider、Tool 或 callback 的调用不会被任意强杀，而是在调用返回后的安全边界停止。

## 4. Agent Intelligence：Planning、Recovery、Skills、MCP 与 Evolution

P2 不另起第二套 Agent loop，而是在现有 `Agent.run() → ToolExecutor → Trace` 主链上增加可组合的 intelligence/runtime state，并把“运行后学习”放到独立 offline pipeline。Planning、Recovery、Skills、MCP 仍受同一 Tool lifecycle、Permission、Cancel 和 Trace 约束，Evolution 也不会在当前 run 内偷偷改写 Agent 行为。

```text
Runtime path

Task
  ↓
PlanningRuntime ───────────────┐
  ↓                            │
Agent.run()                    │
  ├─ SkillRuntime              │
  ├─ RecoveryRuntime ◀─ failure│
  └─ ToolExecutor              │
       ├─ native tools         │
       └─ MCPToolAdapter       │
  ↓
Trace v2 / RunResult / Acceptance

Post-run path

Trace v2 + TrialResult
  ↓
ExperienceMiner
  ↓
Candidate Skill（与正式 catalog 隔离）
  ↓
P2-0 EvaluationHarness
  ↓
PromotionGate
  ↓
显式 promote()
  ↓
<repo>/.agents/skills/
  ↓
现有 SkillCatalog / SkillRuntime
```

### 4.1 Structured Planning

核心文件：`agent/planning.py`、`agent/core.py`。

`planning_mode` 支持：

- `off`：保持基础 ReAct，不创建结构化计划；
- `auto`：对要求测试、多文件提示或明显多阶段描述的任务启用 Planning，简单任务可跳过；
- `always`：每个任务都要求结构化计划。

计划是 typed runtime state，而不是一段自由文本。它包含 step、target、verification 和 version；当 Planning 已启用时，repository-mutating Tool 在必要计划尚未建立或需要 revision 时会被 runtime gate 阻止。计划状态每轮重新注入 model-facing context，因此不依赖历史消息一直保留原文。

### 4.2 Failure-aware Recovery

核心文件：`agent/recovery.py`、`agent/core.py`。

`recovery_mode=structured` 会把已经发生的失败先归一成 `FailureContext`，再由 bounded `RecoveryPolicy` 产生 `RecoveryDecision`。当前策略区分 test/tool/permission/loop/no-progress/completion/infrastructure 等 failure category，并可选择 inspect、rerun test、change approach、replan 或 give up。

Recovery 不是“所有失败都 retry”。Infrastructure failure 保留既有 fatal contract；重复失败受 `recovery_max_attempts` 限制；当当前 plan 已被证据推翻时，Recovery 可以要求 P2-1 创建 `PlanRevision` 后再继续 mutation。

### 4.3 Agent Skills

核心文件：`skills/catalog.py`、`skills/runtime.py`。

Skill 是 filesystem workflow/context capability：

```text
project: <repo>/.agents/skills/<skill-name>/SKILL.md
global : ~/.forge-agent/skills/<skill-name>/SKILL.md
```

project 同名 Skill 覆盖 global。System context 默认只放 Skill metadata；模型通过 `skill_load` 才加载完整 instructions，通过 `skill_reference_load` 再按需读取 `references/`。这形成 progressive disclosure，避免把所有 workflow 全量塞进 prompt。

`scripts/` 只作为 manifest 暴露，Skill subsystem 自己不执行脚本。任何可执行动作仍必须回到正常 Tool/Shell → ToolExecutor → Permission/Hook/Cancel/Trace 链路。

### 4.4 MCP capability integration

核心文件：`mcp_integration/manager.py`、`mcp_integration/adapter.py`、`mcp_integration/registry.py`。

Forge 作为 MCP Host，使用 official MCP Python SDK v2 连接 stdio 或 Streamable HTTP server。远端 Tool 会先被适配成普通 Forge Tool，再注册进现有 `ToolRegistry`：

```text
LLM
 ↓
ToolRegistry
 ↓
ToolExecutor
 ↓
MCPToolAdapter
 ↓
MCPClientManager
 ↓
official MCP SDK
 ↓
MCP Server
```

因此 MCP 不绕过 Hook、Permission、cooperative cancel、Planning effect gate 或 Trace。远端 Tool 使用 `mcp__<server-id>__<tool-name>` 命名；ToolAnnotations 默认不可信，只有 server 配置显式 `trust_read_only_annotations=true` 时，read-only hint 才能降低 permission classification。

### 4.5 Coding Agent Evaluation Harness

核心文件：`evals/coding_agent/`。

P2-0 把 task fixture、grader、trial、metrics、Trace artifact 与 report 固定为统一 harness。当前 architecture variant 已映射为：

```text
baseline_react
planning
planning_recovery
planning_recovery_skills
planning_recovery_skills_mcp
```

Harness 同时记录 outcome 与 process：required grader/acceptance 决定任务成功，Trace 派生 steps、tokens、tool/test、plan/recovery、Skill、MCP 等 metrics。CLI 默认只做 fixture/reference validation 并写 `not_executed`；只有显式 `--real-model` 才会调用配置的真实模型。

### 4.6 Trajectory-driven Skill Evolution

核心文件：`experience/trajectory.py`、`experience/candidate.py`、`experience/evaluation.py`、`experience/promotion.py`、`experience/store.py`。

P2-5 是 post-run offline subsystem，不进入当前任务的 `Agent.run()`。它只从成功且 independent acceptance 已通过的 trajectory 提取 positive workflow，并可消费 P2-2 的 typed failure/recovery event 形成 recovery pattern。

Candidate Skill 保存在 repository-bounded `.forge-agent/experience/`，默认不进入正式 `SkillCatalog`。Candidate 必须先经过 P2-0 baseline vs candidate evaluation，再由 deterministic `PromotionGate` 给出 `PASS / REJECT / INSUFFICIENT_EVIDENCE / EVALUATION_FAILED`。即使 Gate PASS，也只有显式 `PromotionManager.promote()` 才会写入 project Skill；用户手工 Skill没有 Forge evolution provenance 时不会被静默覆盖。

这套机制是 **trajectory-driven + eval-gated improvement**，不是在线 RL、模型参数训练、当前 run 自改 prompt，也不是“Agent 自动越跑越聪明”。

## 5. 模型层：统一 Backend 与能力元数据

核心文件：

- `llm/base.py`：统一 `LLMBackend`、消息、工具 schema、响应类型；
- `llm/router.py`：Provider / Protocol 路由与 runtime capability 注入；
- `llm/capabilities.py`：`ModelCapabilities`；
- `llm/anthropic_backend.py`：Anthropic Messages；
- `llm/openai_compat.py`：OpenAI-compatible Chat Completions；
- `llm/openai_responses.py`：OpenAI Responses；
- `llm/errors.py`：Provider 错误分类。

`create_backend()` 将不同 Provider 统一到 `LLMBackend`，同时把以下概念拆开：

- `context_window`：模型输入+输出总窗口能力；
- `model_max_output_tokens`：模型本身允许的最大输出；
- `max_output_tokens`：Forge 本次请求实际保留的最大输出；
- `context_budget_cap`：Forge 主动设置的 context 上限；
- `context_safety_margin_tokens`：请求前预留安全边界；
- `semantic_packet_max_tokens`：Context semantic side-call 的输入包上限。

未知 OpenAI-compatible 代理不会仅凭模型名伪造 capability。若模型窗口未知，Forge 可以使用配置的 `context_budget_cap` 作为兼容 fallback，但它只代表 Forge policy，不代表模型真实 Context Window。

## 6. Context：从“整个仓库塞进 Prompt”到可预算上下文

`context/` 不只是一个 Repo Map 模块，而是一整套 model-facing context 管理层。

### 6.1 Model-aware Token Budget

核心文件：`context/token_budget.py`、`config/schema.py`、`llm/capabilities.py`。

生产路径的可用输入预算由下面的关系决定：

```text
effective_window = min(model_context_window, forge_context_budget_cap)
                   （若模型窗口未知，则使用 cap fallback）

available_input = effective_window
                  - request_output_reserve
                  - safety_margin
```

这取代了把 `budget_tokens=80000` 当成“模型窗口”的旧语义。

本地请求前计数使用 `TokenCounter`：已知 tiktoken 模型尽量使用 model-aware tokenizer；未知模型使用偏保守的 UTF-8 本地估算。这个估算只用于请求前 budget / compaction 决策；Provider 响应中的 `TokenUsage` 才是请求后的 usage accounting 与 Trace 事实来源。

### 6.2 Persistent Query-aware Repo Map

核心文件：

- `context/repo_map.py`：符号抽取、query-aware 排序与 rendering；
- `context/repo_index.py`：持久化结构索引；
- `context/incremental_repo_map.py`：`PersistentRepoMap`；
- `context/repository_state.py`：HEAD + working tree fingerprint。

`AgentConfig.repo_map_mode` 默认是 `incremental`。第一次针对仓库运行时建立结构索引，后续 run 可复用持久化索引；query 变化时只做 rerank；已知文件发生变化时只刷新对应路径。Repo Map 的目标是提供“结构导航 + 与当前任务相关的代码符号”，而不是把整个仓库源码注入 Prompt。

当工具修改仓库后，Agent 会依据 repository fingerprint / 写入信号让下一轮 Repo Map 失效或增量同步，避免继续使用明显过期的仓库视图。

### 6.3 History 与两阶段 Context Compaction

核心文件：

- `context/history.py`：canonical `ConversationHistory`；
- `context/tool_pruning.py`：Stage A deterministic tool pruning；
- `context/structured_compaction.py`：结构化状态与 semantic summarizer；
- `context/compaction.py`：`TraceableCompaction`。

当前生产接线中，`TraceableCompaction` 由 Chat 入口启用。它不直接改写 canonical history，而是在下一次模型调用前生成 model-facing override：

```text
canonical history
      │
      ├─ request pressure < threshold ───────────────→ 原样给模型
      │
      └─ pressure >= threshold
              │
              ▼
        Stage A deterministic pruning
              │
              ├─ 已降到阈值以下 ───────────────────→ pruned view
              │
              └─ 仍高压
                    │
                    ▼
           Stage B structured / semantic compaction
                    │
                    ├─ semantic 成功 → structured-hybrid-v1
                    └─ semantic 失败 → structured-fallback-v1
```

最近 raw history 会按 token budget 保护；semantic packet 同样按 token 而不是字符截断。Compaction 会生成 checkpoint lineage、before/after token、pressure、pruned event、semantic usage 等 Trace 信息。

重要边界：EventLog 是审计记录，canonical History 是会话事实，compacted view 只是给下一次模型请求使用的派生视图。

## 7. Chat Session：恢复状态与审计日志分离

核心文件：`entry/chat.py`、`agent/session.py`、`agent/session_store.py`。

`ChatSession` 每一轮都创建新 `Task`，但复用同一个 `ConversationHistory` 和 Runner。默认 Session 会持久化到：

```text
logs/chat/<repo_key>/<session_id>/
├── state.json      # 恢复真相源
└── rounds/         # 每轮独立 EventLog
```

`state.json` 保存 History、累计 usage、round metadata、repo revision 与 compaction checkpoint lineage；每轮 JSONL 只负责审计。Session Store 使用临时文件 + `fsync` + `os.replace` 原子更新，并通过 revision + 文件锁避免两个进程静默覆盖同一个 Session。

如果进程在一轮中途退出，`pending_round` 会在恢复时转成明确的 interrupted round，并要求用户检查仓库和 round log，而不是假定上次 Tool side effect 已经完整完成。

## 8. Tool 安全层：所有副作用经过同一生命周期

核心文件：

- `tools/base.py`：Tool / ToolRegistry / ToolResult；
- `harness/executor.py`：统一 Tool lifecycle；
- `harness/permission.py`：ALLOW / CONFIRM / DENY；
- `harness/hooks.py`：PreToolUse / PostToolUse；
- `harness/__init__.py`：产品安全默认 `ToolExecutor`。

冻结生命周期是：

```text
cancel
  → validate tool call
  → pre-hook
  → cancel
  → permission
  → cancel
  → tool
  → post-hook
  → Observation / Trace
  → cancel
```

这条顺序有明确错误语义：

- unknown tool / invalid arguments：在 Hook 和 Permission 之前返回可恢复错误；
- pre-hook block：`HOOK_BLOCKED`；
- pre-hook exception：fail-closed，`HOOK_FAILED`；
- permission deny / 用户拒绝 confirm：`PERMISSION_DENIED`；
- permission 子系统自身异常：升级为 infrastructure failure；
- Tool 普通失败：`TOOL_EXECUTION`，由 Agent 决定是否恢复；
- timeout：保留独立 TIMEOUT 语义；
- post-hook exception：只记 diagnostic，不覆盖已经真实发生的 ToolResult。

### 8.1 路径与命令边界

`PermissionManager` 对 Shell 命令执行 deny / confirm / allow 决策，并可绑定 `workspace` 限制文件工具路径。产品 Runner 默认启用 PermissionManager；isolate 模式额外显式绑定 `workspace=<worktree>`。

文件工具自身也接收 workspace，因此 direct 模式的文件读写不会仅依赖 LLM 自觉提供正确路径。

针对已有文件的局部修改优先使用 `file_edit`：它要求 `old_text` 在目标文件中唯一出现，并基于原始 bytes 做一次 exact replacement，因此未命中的区域不会被重新序列化，能够保留 CRLF/LF、EOF newline、BOM 等原始格式。只有新建文件或确实需要整文件重写时才使用 `file_write`。

Shell effect 采用 fail-safe 分类：只有可证明只读的命令才标记为 read-only；`python -c`、`find`、`awk` 等表达能力较强的命令不再仅凭前缀视为只读。只读 pipeline 仅在每个 stage 都属于明确只读子集时放行，例如 `git diff | cat`；重定向、command substitution、未知 stage 或 mutation-capable stage 仍按 may-mutate 处理。

## 9. Worktree 与 TaskEngine：隔离 Git 工作区，而不是“复制仓库”

核心文件：`task/engine.py`、`runtime/worktree.py`、`agent/orchestrate.py`。

### 9.1 TaskEngine

`TaskEngine` 使用 SQLite WAL 持久化任务状态与 DAG 依赖。认领任务使用原子 `UPDATE ... WHERE ...` 语义，而不是“读 JSON → 修改 → 写回”，从而避免并发 claim 的经典竞态。

隔离运行中主链是：

```text
create_task
 → claim_task(owner="agent")
 → bind_worktree
 → Agent run
 → complete / fail
```

### 9.2 WorktreeSession

`WorktreeSession` 基于：

```text
git worktree add -b wt/<task-name> <path> <base-commit>
```

创建独立 checkout/index/branch。Agent 在这个路径内读写和测试，原始工作树不直接承接这些修改。

结束时 orchestrator 会先 `inspect_changes()`，再应用成果策略：

- `keep-if-changed`：存在未提交修改、提交或状态无法安全判定时保留 worktree；
- `discard`：无论是否产生修改都清理；
- 无成果的 `keep-if-changed` 运行也会清理。

保留 worktree 只代表“成果仍在独立工作树中”，不代表已经 commit、merge、push 或创建 PR。

## 10. Docker Runtime：与 Worktree 是两个正交边界

核心文件：`tools/runtime.py`、`tools/sandbox.Dockerfile`。

`Runtime` 将 Shell / pytest / Git 命令执行从 Tool 实现中抽出来：

```text
ShellTool / PytestTool / GitTool
              │
              ▼
        Runtime.exec()
          ├─ LocalRuntime
          └─ DockerRuntime
```

Docker 默认能力包括：

- 1 GiB memory limit；
- 2 CPU；
- `--network none`；
- `/tmp` tmpfs；
- 可选只读 root filesystem；
- 明确的 bind mount 白名单；
- 运行结束清理容器。

需要区分两种模式：

1. `agent run --sandbox`：命令在 Docker 中执行，但目标 repo 本身仍以 bind mount 暴露给容器，容器内文件/Git 修改会反映到宿主目标 repo；
2. `agent run --isolate --sandbox`：先创建独立 Worktree，再把这个 worktree 以 rw 挂到 `/workspace`，主工作树不作为 Agent 的可写工作区；同时容器 root 只读、网络关闭。

所以 Worktree 解决 Git/文件成果隔离，Docker 解决进程、网络、资源和容器文件系统边界。二者组合才是当前最完整的隔离运行路径，但仍不能描述为“完全安全”。

isolate+sandbox 在调用模型前还会执行 preflight，检查 `git`、`pytest`、worktree 可见性和可写性；preflight 失败被 Runner 归一为 `FAILED / infrastructure_error`。

## 11. Trace v2：把运行过程变成可追溯事实

核心文件：`agent/event_log.py`、`agent/trace_v2.py`。

每次 Run 写 append-only JSONL。Trace v2 为新事件统一提供：

- `trace_schema_version=2`；
- `run_id` / `run_span_id`；
- model / tool / context child span；
- `step_id` 与 operation-specific id；
- entrypoint / session correlation；
- provider error、cancel、infrastructure error、completion rejection；
- acceptance 与 GitHub delivery 结果；
- 写盘边界统一 recursive redaction。

Model span 同时记录本地 `token_breakdown` 和独立 `provider_usage`。前者是请求前诊断 estimate，后者是 Provider 返回的 usage；两者不会混为同一个指标。

EventLog 可以 replay 读取和统计，但它不是确定性执行重放系统。

## 12. Independent Acceptance：Agent 成功之后仍要独立验收

核心文件：`agent/runner.py::AcceptanceContract`。

Acceptance 支持：

- `require_changes`；
- `require_tests`；
- `required_paths`；
- `forbidden_paths`；
- 独立 `verifier(Path) -> bool`。

其中 verifier 由 Runner 持有，不进入 Agent History，因此模型看不到隐藏验收逻辑。结果保存在独立 `acceptance_status` 中：Agent 可以是 SUCCESS，但 Acceptance 仍然失败。

当前 verifier 失败不会自动回灌模型继续修复；这是明确的现有边界。

## 13. GitHub Issue → PR：把模型修改与确定性交付分开

核心文件：`entry/github_issue.py`。

自动 PR 的真实流程是：

```text
GitHub Issue
   ↓
fetch title/body
   ↓
clone / reuse clean local repo
   ↓
create agent/fix-issue-<n>-<timestamp> branch
   ↓
ExecutionRunner → Agent
   │
   └─ 自动 PR 模式下移除 Agent 的 git_add / git_commit 工具
   ↓
Independent Acceptance (--verify-command)
   ↓ passed only
Deterministic Delivery
   ├─ git add --all
   ├─ git commit
   ├─ git push
   └─ GitHub create PR
```

自动 PR 模式有两个重要门禁：

1. 本地仓库启动前必须 clean，避免把用户已有修改一起提交；
2. 必须提供 `--verify-command`，没有独立验收命令时直接拒绝自动交付。

这样 Agent 只负责产生候选修改，commit/push/PR 由确定性代码在验收之后执行。push 或 PR 创建失败时保留已经产生的本地/远端成果，并把 delivery status 写入 Trace。

`--no-pr` 会跳过自动交付，因此不要求 verifier；它仍会创建独立 issue branch 并在本地运行 Agent。

## 14. API：把 isolate 运行暴露为服务

核心文件：`entry/api.py`、`entry/api_store.py`。

FastAPI 层提供任务创建、状态查询、EventLog 查询、SSE 和 cancel。API 任务默认走 isolate 路径，因此每个任务会进入 TaskEngine + WorktreeSession；`sandbox=true` 时再组合 Docker。

API cancellation 使用 `threading.Event` 传到 Agent / Tool lifecycle，是 cooperative cancellation，不会伪装成能够强杀任意同步调用。

API Store 与 Agent TaskEngine 是两个不同状态域：前者服务 HTTP 请求生命周期，后者记录 isolate Agent task 生命周期。

## 15. 关键目录

```text
forge-agent/
├── agent/
│   ├── core.py              # 同步 ReAct、完成性守卫、Reflection、cancel
│   ├── planning.py          # typed Plan / PlanRevision / PlanningRuntime
│   ├── recovery.py          # FailureContext / RecoveryPolicy / RecoveryRuntime
│   ├── runner.py            # 四入口统一执行组合根 + Acceptance
│   ├── orchestrate.py       # isolate 的 async 资源组合根
│   ├── event_log.py         # append-only EventLog
│   ├── trace_v2.py          # Trace schema / correlation / redaction
│   ├── session.py           # Chat Session domain state
│   └── session_store.py     # 原子持久化、revision/lock/redaction
├── context/
│   ├── token_budget.py      # Model-aware TokenBudget / TokenCounter
│   ├── repo_map.py          # Query-aware Repo Map
│   ├── repo_index.py        # 持久化结构索引
│   ├── incremental_repo_map.py
│   ├── history.py           # canonical ConversationHistory
│   ├── tool_pruning.py      # deterministic pruning
│   └── compaction.py        # TraceableCompaction
├── harness/
│   ├── executor.py          # Tool lifecycle
│   ├── permission.py        # ALLOW / CONFIRM / DENY
│   └── hooks.py
├── tools/
│   ├── base.py              # ToolRegistry
│   ├── file_tool.py
│   ├── search_tool.py
│   ├── shell_tool.py
│   ├── test_tool.py
│   ├── git_tool.py
│   ├── runtime.py           # Local / Docker Runtime
│   └── sandbox.Dockerfile
├── task/engine.py           # SQLite WAL TaskEngine
├── runtime/worktree.py      # WorktreeSession / result policy
├── llm/                     # Backend adapters / routing / capabilities / usage
├── skills/                  # P2-3 Skill catalog / progressive disclosure runtime
├── mcp_integration/         # P2-4 MCP client manager / Tool adapter / registration
├── experience/              # P2-5 offline trajectory → candidate → eval → promotion
├── entry/
│   ├── cli.py               # run/chat/log
│   ├── chat.py              # 多轮 ChatSession
│   ├── api.py               # HTTP API
│   └── github_issue.py      # Issue → Acceptance → PR
├── evals/
│   ├── coding_agent/        # P2-0 EvaluationHarness / grader / report
│   └── fixtures/            # deterministic task / Skill / MCP / evolution fixtures
├── docs/
│   ├── evidence/README.md   # Claim → Evidence → Result → Limitation
│   └── changes/             # 每轮改动记录
├── README.md
└── USAGE.md
```

## 16. 功能矩阵与当前边界

| 能力 | run | chat | API | GitHub Issue |
| --- | --- | --- | --- | --- |
| 统一 ExecutionRunner | ✓ | ✓ | ✓ | ✓ |
| Persistent Query-aware Repo Map | ✓ | ✓ | ✓ | ✓ |
| Model-aware Token Budget | ✓ | ✓ | ✓ | ✓ |
| Structured Planning | ✓ | ✓ | ✓ | ✓ |
| Failure-aware Recovery | ✓ | ✓ | ✓ | ✓ |
| Agent Skills | ✓ | ✓ | ✓ | ✓ |
| MCP Tool integration | ✓ | ✓ | ✓ | ✓ |
| Trace v2 | ✓ | ✓（每轮） | ✓ | ✓ + delivery |
| 持久化 Session | — | ✓ | — | — |
| Traceable Context Compaction | — | ✓ | — | — |
| Git Worktree isolate | `--isolate` | — | 默认 | — |
| Docker Runtime | `--sandbox` | `--sandbox` | 可选 | — |
| isolate + Docker | ✓ | — | 可选 | — |
| Independent Acceptance | Runner 可用 | Runner 可用 | Runner 可用 | 自动 PR 强制 verifier |
| 自动 commit / push / PR | — | — | — | ✓ |

P2-0 Evaluation Harness 与 P2-5 Skill Evolution 是独立的 evaluation/post-run 能力，不是四入口上的额外执行模式。Evolution 的正式生效仍通过现有 project SkillCatalog。

明确没有实现或不应夸大的能力包括：多 Agent、parallel/multi-tool call、任意同步调用强制终止、自动 merge、无人监督发布、分布式 Session Service、完整 OpenTelemetry、Context recall C6、在线 RL/模型参数训练、自动 Skill promotion。

## 17. 安装

环境要求：Python 3.11+、Git；使用 Docker 路径时需要 Docker daemon。

```bash
git clone https://github.com/Napabana/forge-agent.git
cd forge-agent
git checkout dev

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"

agent --help
```

需要 FastAPI：

```bash
pip install -e ".[api,dev]"
```

需要 tiktoken 和更多 tree-sitter language bindings：

```bash
pip install -e ".[full,dev]"
```

## 18. 配置：推荐显式区分模型能力与 Forge policy

`config/default.yaml` 仍兼容旧字段。新配置建议使用清晰语义：

```yaml
llm:
  provider: openai
  protocol: chat_completions       # auto | chat_completions | responses
  model: your-model
  api_key: ${YOUR_API_KEY}
  base_url: https://api.example.com/v1

  # 只有在你确认当前模型/渠道的真实能力时才填写：
  context_window: 128000
  model_max_output_tokens: 8192

  # Forge 本次请求的输出 reserve：
  max_output_tokens: 8192

agent:
  max_steps: 40
  context_budget_cap: 80000
  context_safety_margin_tokens: 1024
  log_dir: ./logs

  # P2 runtime capabilities
  planning_mode: auto            # off | auto | always
  recovery_mode: structured      # off | structured
  recovery_max_attempts: 4
  skills_enabled: true
  skills_global_dir: ~/.forge-agent/skills
  skills_max_loaded: 3

context:
  repo_map_budget: 8000
  history_window: 20
  semantic_packet_max_tokens: 16000

mcp:
  enabled: false
  servers: []
```

如果是未知 OpenAI-compatible 代理，不确定 Context Window 时应省略 `context_window` / `model_max_output_tokens`，保留 `context_budget_cap` 作为 Forge fallback。

兼容字段：

- `llm.max_tokens` → 兼容解释为 request `max_output_tokens`；
- `agent.budget_tokens` → 兼容解释为 `context_budget_cap` fallback。

API Key 应通过环境变量或 `FORGE_ENV_FILE` 指定的仓库外 env 文件加载，不应把真实 secret 提交到仓库。

## 19. 快速开始

一次性任务：

```bash
agent run --repo /path/to/project --task "修复失败的测试并运行相关 pytest"
```

多轮 Chat：

```bash
agent chat --repo /path/to/project
```

独立 Worktree：

```bash
agent run --repo /path/to/project \
  --task "修复一个明确问题并运行测试" \
  --isolate --result-policy keep-if-changed
```

Worktree + Docker：

```bash
agent run --repo /path/to/project \
  --task "修复并验证测试" \
  --isolate --sandbox
```

GitHub Issue 本地修复：

```bash
python -m entry.github_issue \
  --repo owner/repo --issue 42 --local-path /tmp/repo --no-pr
```

GitHub Issue 自动 PR：

```bash
export GITHUB_TOKEN=...
python -m entry.github_issue \
  --repo owner/repo \
  --issue 42 \
  --local-path /tmp/repo \
  --verify-command "python -m pytest -q tests/test_target.py"
```

完整逐步验收见 [`USAGE.md`](USAGE.md)。

## 20. 证据与项目声明边界

本仓库把“实现事实”“离线确定性回归”“冻结 benchmark”“真实模型小样本”和“真实端到端案例”分开记录。所有可引用数字统一以 [`docs/evidence/README.md`](docs/evidence/README.md) 为入口。

当前可复现证据包括 Context B1、Repo Map retrieval benchmark、Persistent Repo Map phase benchmark、B2 real-model small sample、Failure Harness、Trace/Runner contract，以及一个真实 Issue → merged PR 案例。它们各自只能证明对应协议和样本范围，不能外推为总体 Coding Agent 成功率或线上 SLA。

默认离线证据校验：

```bash
python -m evals.verify_evidence_pack
```

全量回归：

```bash
python -m pytest -q
```
