# 11. Long-horizon Runtime：持久化执行、上下文归档与恢复架构

> 文档状态：完整设计合同，待实现。
>
> 源码基线：Napabana/forge-agent，dev，HEAD = f6f5b5616f3b287dd4286d7377b116582948b7af（2026-11-19）。
>
> 对标基线：hmmgotit/RuoCun，main，HEAD = 2e7eaa3a7d76c11a437c4e5c4ca26443ffa851f4。
>
> 本文定义目标架构、持久化数据合同、运行状态机、模块接口、恢复流程、CLI、配置、测试和验收标准。实现以本文为统一合同，源码完成后再回填实际路径与符号。

## 1. 建设目标

Forge Agent 已经具备 ReAct 主循环、Structured Planning、Failure-aware Recovery、Tool Lifecycle、Context Compaction、Chat Session、Skills、MCP、Evaluation Harness、Git Worktree、Docker 和 Project Memory。

本阶段建设 Long-horizon Runtime，使一次 Agent 执行具备跨多轮模型请求、上下文压缩、工具执行、进程退出和进程重启的连续状态管理能力。

目标能力：

1. **Durable Run State**：持久化运行状态、执行检查点、模型动作和运行时状态，支持进程重启后恢复。
2. **Persistent Tool Journal**：在副作用工具执行前提交 Intent，在工具返回后提交 Result，记录最终参数、授权依据和操作状态。
3. **Unknown Outcome Recovery**：准确识别结果未知的操作，执行证据核查、人工核实和受控重试。
4. **Raw Context Archive + ArchiveRead**：持久化原始消息和工具结果，保存摘要来源关系，允许模型按需读取历史原文。
5. **Prefix Cache Audit**：记录模型请求前缀指纹、组件变化和 Provider 缓存用量，定位缓存命中变化的原因。
6. **First-run Onboarding**：首次启动完成 Provider、模型、凭据、工作区信任和运行环境配置。
7. **Skill / MCP Resource Lifecycle**：完善 Skill 与 MCP 的安装来源、版本锁定、测试、更新、启停和移除管理。

统一设计原则：

- ExecutionRunner 是四入口统一执行组合根。
- Durable Runtime Store 是运行状态、操作结果和上下文归档的持久化事实源。
- EventLog 继续负责 Trace、审计和可视化，不承担关键状态恢复。
- ToolExecutor 继续负责所有工具的 Validation、Hooks、Permission、Cancel 和错误分类；Durable Tool Journal 接入同一生命周期。
- Canonical History、Model Context、Context Archive 和 Project Memory 保持独立职责。
- 所有恢复行为由持久化状态、操作契约和可验证证据驱动。
- 每个关键状态转换都具有稳定身份、Revision、Owner Epoch 和原子提交边界。

## 2. 当前架构基线与改造位置

### 2.1 已有模块

| 当前模块 | 已有职责 | 本阶段扩展 |
|---|---|---|
| agent/runner.py | 四入口执行组合、direct/isolate、Acceptance | 创建和恢复 Durable Run，管理 Store 生命周期 |
| agent/core.py | ReAct、History、Planning、Recovery、Tool 调用 | 持久化模型动作、运行时检查点和 Observation |
| harness/executor.py | Validation、PreToolUse、Permission、Cancel、Tool、PostToolUse | 接入 Intent、Started、Result 的持久化提交 |
| tools/base.py | ToolRegistry、ToolResult、ToolEffect | 增加重放策略与工具恢复元数据 |
| agent/session.py | ChatSessionState、PendingRoundState、ChatRoundState | 关联 run_id、恢复状态和 Store Revision |
| agent/session_store.py | JsonChatSessionStore 原子保存、文件锁、Revision、redaction | 保留兼容和会话元数据；活动 Run 的中间检查点转由 Durable Runtime Store 管理 |
| context/history.py | 完整逻辑历史 | 继续作为进程内 Canonical History，写入 Archive 投影 |
| context/compaction.py | Stage A pruning、structured compaction、checkpoint lineage | 将原文归档、Summary 和 Model View 持久化 |
| context/structured_compaction.py | 确定性证据、语义摘要和 fallback | 为摘要生成 Archive 来源引用 |
| agent/event_log.py | JSONL EventLog、Trace v2、correlation、redaction | 增加 Durable Runtime 事件引用；不承担恢复真相 |
| llm/usage.py | TokenUsage、SessionUsage | 复用 Provider 缓存 Token 字段 |
| project_memory/ | 项目级语义知识、检索、候选和收口 | 保持独立，不存放原始对话和工具执行状态 |
| entry/resource_commands.py | skill add/list、mcp add/list | 增加资源来源锁定和完整生命周期命令 |
| entry/cli.py | CLI 入口、config、memory、skill、mcp 命令 | 增加 init、runtime、archive、cache、doctor 命令 |
| config/schema.py | LLM、Agent、Context、MCP、Memory 配置 | 增加 Durable Runtime、Cache Audit、Onboarding 配置 |

### 2.2 关键现状

- ConversationHistory 保存完整逻辑消息；TokenBudget 和 TraceableCompaction 构建模型可见视图。
- TraceableCompaction 的 CompactionEntry 和 ActiveCompactedView 目前主要保存在运行时对象中；restore_checkpoint_lineage 只恢复 checkpoint cursor，不恢复旧 Summary 正文。
- ChatSession 在每轮开始前写 pending_round，结束后保存 state.json；进程中断后目前只能追加 interrupted 提示，要求检查仓库和 round log。
- JsonChatSessionStore 已具备文件锁、Revision、临时文件、fsync、replace 和 secret redaction。
- ToolExecutor 的执行顺序是 cancel、validate、PreToolUse、permission/confirm、cancel、invocation callback、registry.execute_tool、PostToolUse。
- TOOL_INVOCATION_STARTED 是进入实际 Tool 调用前的边界；TOOL_EXECUTION_STARTED 是 Agent 侧的生命周期 Trace，不能代替持久化 Intent。
- EventLog JSONL 每次 append 会 flush，并由 Trace v2 负责 redaction；它是审计日志，不是事务状态存储。
- TokenUsage 已有 input_tokens、cached_tokens、cache_write_tokens、output_tokens、reasoning_tokens、estimated 和 cache_read_reported。
- TaskEngine 已使用 SQLite WAL 和原子事务管理 isolate 任务认领；Project Memory 已使用跨进程文件锁、临时文件、fsync 和 replace。
- Skill CLI 已有 skill add/list；MCP CLI 已有 mcp add/list，均复用用户级 config.yaml 和现有资源目录。

### 2.3 RuoCun 对标范围

本设计吸收 RuoCun 的以下实现合同：

| RuoCun 路径 | 吸收的设计 |
|---|---|
| src/ruocun/session/contract.py | DbOwner、owner_epoch、RunRef、ToolRef、Intent、ToolRecord、Checkpoint、ArchiveRewrite 等数据契约 |
| src/ruocun/session/durable_store.py | SQLite WAL、事务内 Owner 校验、原子 Intent/Result、恢复视图、未知结果处理 |
| src/ruocun/session/recovery.py | 稳定工具引用、恢复决策和工具消息配对修复 |
| src/ruocun/session/archive.py | 不可变归档、内容哈希、来源闭包、Projection、显式 ArchiveRewrite |
| src/ruocun/tools/archive_read.py | 绑定当前执行范围的分页归档读取 |
| src/ruocun/session/cache_audit.py | Prefix 变化、Provider Usage、缓存命中率和空闲时间审计 |
| src/ruocun/agent/context_runtime.py | 请求预算、缓存诊断和压缩提交顺序 |
| src/ruocun/tui/onboarding/ | 首次配置、凭据验证、语言、工作区信任和初始化流程 |

Forge 的实现统一使用 Python、SQLite WAL、现有 ToolExecutor、现有 ExecutionRunner 和现有 Context/LLM 抽象。

## 3. 总体架构

### 3.1 组件关系

    CLI / Chat / API / GitHub Issue / Evaluation
                       |
                       v
                ExecutionRunner
                       |
             +---------+---------+
             |                   |
             v                   v
       DurableRunManager     Existing Orchestrator
             |                   |
             +---------+---------+
                       |
                       v
              DurableRuntimeStore
                       |
       +---------------+----------------+
       |               |                |
       v               v                v
   Run State       Operation Journal  Context Archive
   Checkpoints     Intent / Result    Raw / Summary / Prune
       |               |                |
       +---------------+----------------+
                       |
                       v
                 Agent Runtime
                       |
          +------------+-------------+
          |            |             |
          v            v             v
       Planning     Recovery     Context Runtime
          |            |             |
          +------------+-------------+
                       |
                       v
                  ToolExecutor
                       |
          Validation / Hooks / Permission
                       |
                Commit Intent
                       |
                 Mark Started
                       |
                  Tool.execute
                       |
                Commit Result
                       |
                Append Observation
                       |
                Commit Checkpoint

LLM 请求由 Provider Backend 在实际请求构建边界生成 RequestFingerprint。请求返回后，Cache Audit 与 TokenUsage 一起写入 DurableRuntimeStore。

首次启动由 Onboarding 初始化用户配置、凭据引用和 WorkspaceTrust。Skills/MCP 作为资源管理子系统，继续进入现有 Agent Runtime。

### 3.2 Durable Runtime Store 的存储位置

默认路径：

    ~/.forge-agent/runtime/<project_id>/runtime.db
    ~/.forge-agent/runtime/<project_id>/blobs/
    ~/.forge-agent/runtime/<project_id>/locks/

project_id 复用 ProjectIdentityResolver 的身份规则：

1. 优先使用规范化 Git origin 生成项目身份。
2. 无 origin 时使用 Git common dir。
3. 非 Git 仓库使用规范化绝对路径。
4. 同一 Git 仓库的 clone/worktree 按现有 ProjectIdentityResolver 规则归属同一项目。

配置中的 durable_runtime.root 可覆盖根目录，但必须是绝对路径。

数据库目录权限为 0700，数据库、WAL、SHM、锁和 Blob 文件权限为 0600。数据库启用 WAL、foreign_keys、busy_timeout 和 synchronous=FULL。写事务使用 BEGIN IMMEDIATE，所有关键提交执行事务级 Owner Epoch 与 Revision 校验。

### 3.3 Scope 设计

一个 Project 可以包含多个 Session 和 Run。

- Chat Session：scope_id = session_id；同一 Session 的多个 round 使用不同 run_id。
- CLI run：scope_id = run_id。
- API task：scope_id = run_id，api_task_id 与 run_id 建立关联。
- GitHub Issue：scope_id = run_id，issue number、branch、worktree 和 delivery operation 关联到该 Run。
- Evaluation Trial：scope_id = run_id；每个 Trial 使用独立 scope，不跨 Trial 共享 History、Archive Projection、Cache Comparator 或 Project Memory。
- Supervisor Worker：worker_run_id 独立，parent_run_id 指向 Lead；继承所属 Session/Run 的 scope_id 和 Owner Epoch。

ArchiveRead 只读取当前 Run 被授权的 scope。Chat Session 的跨 round Archive 由同一 session_id 关联。

## 4. DurableRuntimeStore 数据模型

### 4.1 数据表

| 表 | 主要字段 | 职责 |
|---|---|---|
| runtime_meta | key、value_json | schema_version、project_identity、store_format、迁移信息 |
| runtime_scopes | scope_id、owner_instance_id、owner_epoch、host_id、boot_id、pid、process_start、revision | Scope 所有权和写入代次 |
| runs | run_id、task_id、parent_run_id、session_id、scope_id、entrypoint、status、repo_identity、repo_root、worktree_path、config_digest、tool_registry_digest、checkpoint_id、step、revision、owner_epoch、timestamps | Run 生命周期 |
| run_checkpoints | checkpoint_id、run_id、revision、kind、payload_json/blob_ref、payload_digest、owner_epoch、created_at | 完整可恢复检查点 |
| model_calls | model_call_id、run_id、step_id、request_digest、request_fingerprint、status、action_json、usage_json、provider_request_id、created_at、finished_at | 模型请求和动作持久化 |
| operations | operation_id、run_id、scope_id、parent_operation_id、step_id、action_event_id、tool_call_id、operation_kind、name、args_json/blob_ref、args_digest、effect、replay_policy、permission_json、approval_ref、policy_digest、state、result_json/blob_ref、observation_json、result_digest、owner_epoch、timestamps | Tool 和外部副作用操作日志 |
| recovery_decisions | decision_id、run_id、operation_id、action、actor、explanation、evidence_json、request_digest、created_at、owner_epoch | 不可变恢复决策 |
| archive_records | archive_id、scope_id、sequence、kind、payload_json/blob_ref、payload_digest、source_ids_json、previous_id、redaction_version、created_at | 不可变上下文归档 |
| archive_projections | scope_id、projection_kind、revision、lineage_json、messages_digest、updated_at | Canonical 与 Model View 投影 |
| cache_audit_events | request_id、run_id、session_id、step_id、provider、model、protocol、prompt_tokens、cached_tokens、cache_write_tokens、cache_read_reported、estimated、component_hashes_json、changed_components_json、prefix_hash、idle_ms、created_at | Prefix Cache Audit |
| outbox_events | event_id、run_id、seq、event_type、payload_json、created_at、delivered_at | 持久化事件投递 |
| workspace_trust | project_id、repo_identity、granted_scopes_json、trust_version、trusted_at、revoked_at | 工作区信任状态 |

所有表使用稳定主键。run_id、operation_id、archive_id、decision_id 和 request_id 使用 UUID 或具备等价唯一性的随机标识。

### 4.2 SQLite 运行合同

数据库打开时执行：

- PRAGMA journal_mode=WAL，并核验实际返回值。
- PRAGMA synchronous=FULL。
- PRAGMA foreign_keys=ON。
- PRAGMA busy_timeout = configured timeout。
- 对数据库、WAL、SHM 和目录权限执行核验。
- 读取 PRAGMA user_version，并校验产品标记、Project Identity 和 Schema。
- 使用 PRAGMA integrity_check 执行显式诊断。

所有写事务通过统一 transaction() helper：

1. BEGIN IMMEDIATE。
2. 校验 scope 当前 Owner Instance 与 Owner Epoch。
3. 校验 expected_revision。
4. 校验数据身份、状态转换和外键。
5. 写入所有关联记录。
6. 更新 scope revision。
7. COMMIT。
8. 返回带 revision 的提交回执。

任一步骤失败执行 ROLLBACK。提交回执包含 operation_id、revision、owner_epoch 和事务结果。

数据库版本只允许向前迁移。未知版本、身份不匹配和损坏数据库均保留原文件并输出明确诊断；迁移必须先创建可恢复备份。

### 4.3 Owner 与并发写入

定义：

    ProcessIdentity:
        host_id
        boot_id
        pid
        process_start_time

    ScopeOwner:
        scope_id
        instance_id
        owner_epoch
        process_identity

Claim 流程：

1. 取得 Scope 对应的 OS 文件锁。
2. BEGIN IMMEDIATE。
3. 读取现有 Owner。
4. 如果原 Owner 对应进程仍存活，返回 OwnerConflict。
5. 如果原进程已退出，owner_epoch 递增。
6. 写入新的 instance_id、owner_epoch 和 ProcessIdentity。
7. 在同一事务中将该 Scope 下未完成的 started operation 标记为 outcome_unknown。
8. COMMIT 并返回 Owner Receipt。

所有写操作在事务内部校验 instance_id 与 owner_epoch。旧进程的写入因 Owner Epoch 不匹配而被拒绝。

ProcessIdentity 使用 host、boot、PID 和进程创建时间共同识别进程，避免 PID 重用。跨主机数据库副本不执行自动接管；host_id 或 boot_id 不一致时进入显式恢复流程。

同一 Project 的不同 scope 可以并发执行；SQLite 负责事务串行化。相同 Chat Session 的同一时刻只有一个有效 Owner。

## 5. Durable Run State 与 Checkpoint

### 5.1 Run 状态

Agent RunStatus 保留现有 SUCCESS、FAILED、CANCELED、INCOMPLETE、GAVE_UP 等业务结果。

Durable Run Lifecycle 单独记录执行过程：

| 状态 | 含义 |
|---|---|
| created | Run 已创建，尚未进入 Agent 主循环 |
| running | 当前 Owner 正在执行 |
| paused | 用户或调度器请求暂停，已保存检查点 |
| interrupted | 进程退出，存在可恢复检查点 |
| reconciliation_required | 存在 outcome_unknown operation |
| agent_finished | Agent 已返回 RunResult |
| acceptance_pending | Runner 正在执行 Independent Acceptance |
| acceptance_passed | Independent Acceptance 已通过 |
| acceptance_failed | Independent Acceptance 未通过 |
| delivery_pending | 已授权执行交付操作 |
| delivered | GitHub/外部交付已确认完成 |
| completed | Run 生命周期结束 |
| failed | 执行框架或任务失败 |
| canceled | Run 被取消 |
| incomplete | 资源预算耗尽或执行未完成 |

状态转换：

    created -> running
    running -> paused
    running -> interrupted
    running -> reconciliation_required
    running -> agent_finished
    agent_finished -> acceptance_pending
    acceptance_pending -> acceptance_passed / acceptance_failed
    acceptance_passed -> delivery_pending -> delivered
    agent_finished / acceptance_passed / acceptance_failed -> completed
    running -> failed / canceled / incomplete

终态记录保留原始 RunResult、Acceptance 和 Delivery 的独立结果，不把 Agent SUCCESS 等同于 Acceptance 成功或 PR 已交付。

### 5.2 RunCheckpoint 结构

每个检查点使用带版本的 JSON 数据结构：

    RunCheckpoint:
        schema_version
        checkpoint_id
        run_id
        scope_id
        revision
        step
        next_step

        canonical_history:
            role
            content
            event_ref
            tool_call_id
            responses_call

        pending_action:
            action
            action_event_id
            model_call_id
            logical_tool_call_id

        planning_state
        recovery_state
        skill_runtime_state
        loop_detector_state
        semantic_progress_state

        usage
        usage_complete

        completion_guard_state:
            test_attempted
            last_test_passed
            last_test_error_type
            last_successful_test_content_state
            initial_repo_content_state
            completion_checks

        repository_state:
            repo_revision
            repo_content_fingerprint
            repo_map_revision
            worktree_path

        context_state:
            canonical_archive_revision
            model_view_revision
            active_summary_id
            compaction_checkpoint_ids

        execution_config_digest
        tool_registry_digest
        model_config_identity
        created_at

History 序列化必须保留 LLMMessage 的 role、content、event_ref、tool_call_id 和 responses_call。Responses 原生 function_call 与 function_call_output 的配对关系必须完整保留。

PlanningRuntime、RecoveryRuntime、SkillRuntime、LoopDetector、SemanticProgressTracker 和 Completion Guard 均提供 snapshot()/restore()。每个 snapshot 带 schema_version，未知版本拒绝恢复并保留原检查点。

### 5.3 Checkpoint 提交时机

必须在以下边界持久化：

1. Run 创建并取得 Owner 后。
2. 模型请求开始前：保存 model_call_id、request_digest 和请求指纹。
3. 模型返回并解析 Action 后：原子保存 response action、usage 和 pending_action。
4. Planning/Recovery/Skill control action 应用后。
5. Tool Result 已提交且 Observation 进入 Canonical History 后。
6. 每轮 Context Prepare/Compaction 提交后。
7. FINISH、GIVE_UP、CANCELED、FAILED、INCOMPLETE 等终止结果形成后。
8. Independent Acceptance 完成后。
9. GitHub commit、push、PR 等交付操作完成后。

Checkpoint 采用增量记录和周期性快照。恢复所需的最近完整快照与其后的事件必须能够重建当前状态。默认每个 Agent step 至少产生一个可恢复检查点；工具调用边界使用独立事务。

### 5.4 模型请求恢复

model_calls 状态：

- started：请求已发出，尚无持久化响应。
- response_committed：解析后的 Action 和 Usage 已持久化。
- outcome_unknown：进程退出时没有可验证的响应提交。

恢复规则：

- response_committed：直接恢复已保存 Action，不再次调用模型。
- started/outcome_unknown：保留该请求的未知用量记录，使用已提交的 Canonical History 发起新的 model_call_id。
- 新请求必须重新计算 RequestFingerprint，并单独记录 Provider Usage。
- 不把未知 Usage 记为 0；SessionUsage 的 unknown_cache_read_calls 和 estimated_calls 继续表达统计完整性。

模型响应必须在进入 ToolExecutor 之前提交到 Store。这样进程重启后能够恢复模型已经给出的 ToolCall，而不是重新向模型询问并产生不同动作。

## 6. Persistent Tool Intent / Result

### 6.1 Tool Operation 数据结构

    ToolIntent:
        operation_id
        run_id
        scope_id
        step_id
        action_event_id
        logical_tool_call_id
        tool_name
        arguments_digest
        arguments_payload_ref
        effect
        replay_policy
        permission_decision
        approval_ref
        policy_digest
        owner_epoch
        created_at

    ToolOperation:
        operation_id
        state
        started_at
        finished_at
        result
        observation
        result_digest
        recovery_decision_id

arguments_digest 使用 canonical JSON + SHA256。arguments_payload_ref 指向经过 redaction 和安全存储处理的参数载荷。

敏感字段遵循 agent/trace_v2.py 的字段级 redaction 规则。凭据应使用环境变量引用或凭据引用，不将明文 API Key、Authorization、password、refresh token 写入 Trace、Archive、Cache Audit 或普通参数日志。被识别为敏感且无法安全恢复的参数标记 replayable=false。

### 6.2 Tool Operation 状态机

    intent_committed
          |
          v
        started
          |
          +-----------------> result_committed
          |
          +-----------------> outcome_unknown
                                  |
                 +----------------+----------------+
                 |                |                |
                 v                v                v
          resolved_applied  resolved_not_applied  accepted_unknown

状态语义：

| 状态 | 语义 | 自动执行策略 |
|---|---|---|
| intent_committed | 最终授权和参数已提交，尚未进入 Tool 调用边界 | 可按原 Intent 继续；授权策略变化时重新确认 |
| started | 已提交 Tool 即将开始执行 | 进程接管时转 outcome_unknown |
| result_committed | ToolResult、Observation 和结果摘要已提交 | 直接恢复原结果，不再执行 |
| outcome_unknown | Tool 可能已经产生副作用，但没有可信 Result | 进入恢复流程 |
| resolved_applied | 已有证据确认操作生效 | 恢复已确认结果，不再执行 |
| resolved_not_applied | 已有证据确认操作未生效 | 创建新的 operation_id 后执行新尝试 |
| accepted_unknown | 用户确认接受未知结果并记录说明 | 保留未知事实，禁止静默重复该操作 |

同一 run_id + logical_tool_call_id 只能对应一个 operation_id。相同身份但参数摘要、工具名称、权限决策或 policy_digest 不一致时，提交失败。

### 6.3 ToolExecutor 集成顺序

ToolExecutor 保留当前生命周期，在 Permission/Confirmation 完成后接入 Journal：

1. Cooperative Cancel。
2. ToolRegistry.validate_tool_call。
3. PreToolUse Hook。
4. Cooperative Cancel。
5. PermissionManager.check。
6. 用户确认并生成 Approval Receipt。
7. Cooperative Cancel。
8. Durable Journal.commit_intent。
9. Durable Journal.mark_started。
10. EventLog 写 TOOL_INVOCATION_STARTED。
11. ToolRegistry.execute_tool。
12. PostToolUse Hook。
13. Durable Journal.commit_result，原子保存 ToolResult、Observation 和 outbox event。
14. Agent 将 Observation 追加到 Canonical History。
15. Durable Store 提交包含新 History 和 Runtime Snapshot 的 RunCheckpoint。

关键规则：

- Permission DENY、PreToolUse block 和 Hook failure 不创建已批准的 Intent。
- CONFIRM 必须取得真实 Approval Receipt 后才能提交 Intent。
- Intent 提交失败时 Tool 不执行。
- Started 提交失败时 Tool 不执行。
- mark_started 必须先于 TOOL_INVOCATION_STARTED 和 Tool.execute。
- Tool 已执行但 Result 提交失败时，停止 Agent 后续模型请求；Run 进入 reconciliation_required，operation 保持 started，后续恢复转为 outcome_unknown。
- Result 已提交但 History/Checkpoint 尚未提交时，恢复流程从 operation.result 和 operation.observation 补齐 History，不再调用 Tool。
- PostToolUse 仍然是副作用发生后的观察 Hook，其异常写入 diagnostics，不覆盖真实 ToolResult。
- 所有 MCP Tool 继续通过同一 ToolExecutor 和 Journal。

### 6.4 Tool Call 身份

优先使用 Provider 原生 tool_call_id 或 Responses call_id。

没有 Provider call ID 时，使用 action_event_id 生成 logical_tool_call_id：

    logical_tool_call_id = run_id + ":" + action_event_id

operation_id 由 Runtime 生成 UUID。Provider call ID、Action Event ID 和 operation_id 分别保存，避免混用。

### 6.5 Replay Policy

在 ToolMetadata 中增加 replay_policy：

| 策略 | 语义 |
|---|---|
| READ_ONLY | 无外部副作用，可创建新 operation 重新读取 |
| IDEMPOTENT | 工具合同明确支持相同参数/幂等键重复执行 |
| NON_IDEMPOTENT | 重复执行可能产生重复副作用 |
| UNKNOWN | 未声明或无法证明重放语义 |

默认 UNKNOWN。ToolEffect 继续表示 READ_ONLY/MAY_MUTATE；Replay Policy 单独表达重放语义。

工具清单：

- file_read、file_view、search_text、find_files、find_symbol、archive_read 等纯读取工具声明 READ_ONLY。
- file_write、file_edit、shell、git、test 和 MCP 工具按具体操作声明 Replay Policy；无法静态确定时使用 UNKNOWN。
- MCP ToolAnnotations 只用于现有 effect 识别；幂等策略必须由用户配置或可信工具合同显式声明。
- ShellTool 不通过字符串猜测命令幂等性；未识别为明确只读合同的命令按 UNKNOWN 处理。
- IDEMPOTENT 工具必须支持稳定 idempotency_key，并将同一 key 传到执行端。

## 7. Unknown Outcome Recovery

### 7.1 产生条件

Scope 接管时，所有 state=started 且没有 result_committed 的 Operation 原子更新为 outcome_unknown。

典型场景：

- Tool 已经调用外部 API，响应返回前进程退出。
- Git push 已经到达远端，但本地没有保存命令结果。
- GitHub PR 已创建，但响应没有写入本地状态。
- MCP Server 已处理请求，但连接中断。
- 本地文件写入完成，但进程在 Result Commit 前退出。

### 7.2 恢复检查顺序

恢复 Run 时：

1. 取得新的 Scope Owner 和 owner_epoch。
2. 校验 Project Identity、repo_root、worktree_path 和 Run Config Digest。
3. 读取最近 RunCheckpoint。
4. 枚举所有未完成 Operation。
5. 对 started 标记 outcome_unknown。
6. 读取已有 result_committed 结果并补齐 Observation。
7. 检查所有 outcome_unknown 操作。
8. 生成 RecoveryView，列出 operation_id、工具名称、参数摘要、effect、Replay Policy、开始时间和可用证据。
9. 存在未决副作用时，将 Run 标记 reconciliation_required。
10. 未决操作解决后，从最近检查点继续执行。

### 7.3 Reconciler 接口

    ToolReconciler.inspect(intent) -> ReconciliationResult

    ReconciliationResult:
        status: APPLIED | NOT_APPLIED | UNKNOWN
        evidence_type
        evidence
        observed_at
        source_digest

Reconciler 是只读核查接口，不执行原操作。

第一批内置 Reconciler：

| 工具/操作 | 核查方式 |
|---|---|
| file_write | 读取目标文件，比较目标内容摘要 |
| file_edit | 检查目标文件当前摘要及预期变更内容 |
| git commit | 查询 HEAD、commit SHA 和提交内容 |
| git push | 查询远端 ref 与本地目标 commit |
| GitHub create PR | 按 repository、head branch、base branch 查询已有 PR |
| GitHub push/merge | 查询远端分支、PR 状态和 commit SHA |
| MCP | 仅使用 server 显式提供的只读查询合同 |
| shell | 通用 Shell 不提供自动语义核查，使用人工核实或具体工具 Reconciler |

Reconciler 返回的证据与 source_digest 一起持久化。核查结果 UNKNOWN 时保持 outcome_unknown。

### 7.4 Recovery Decision

RecoveryDecision：

    decision_id
    operation_id
    action
    actor
    explanation
    evidence
    created_at
    request_digest

action：

- inspect：执行只读核查并追加证据。
- resolve_applied：确认操作已经生效，保存结果回执。
- resolve_not_applied：确认操作未生效，允许创建新的 operation_id。
- authorize_retry：针对具有明确幂等合同的操作，授权使用同一 idempotency_key 创建新尝试。
- accept_unknown：用户记录并接受未知结果，操作保持不可重复执行的约束。

decision_id 幂等。同一 decision_id 对应不同 request_digest 时拒绝提交。Recovery Decision 不覆盖原始 ToolRecord，只追加决策记录和派生状态。

### 7.5 未决操作对 Agent 的约束

存在 outcome_unknown 的副作用操作时：

- 允许 ArchiveRead、file_read、git_status、git_diff、只读 Reconciler 和其他明确 READ_ONLY 操作。
- 阻止新的 MAY_MUTATE ToolCall。
- Agent Runtime 注入明确的 Recovery Context，列出待核实 operation_id 和当前阻断原因。
- 只有 Recovery Manager 提交有效决策后才解除对应限制。
- 不把 Unknown Outcome 当成普通 TOOL_FAILURE，不进入现有 RecoveryPolicy 的自动 RETRY 分支。

## 8. Context Archive 与 ArchiveRead

### 8.1 四种上下文数据

| 数据 | 职责 | 生命周期 |
|---|---|---|
| Canonical History | 完整逻辑对话 | Session/Run 生命周期 |
| Model Context | 当前实际发送给模型的上下文 | 每次请求重建，可压缩 |
| Context Archive | 原始消息、工具结果和派生摘要的不可变证据 | 长期持久化 |
| Project Memory | 项目架构、决策、约束和经验 | 跨 Run 复用 |

Project Memory 保存经过整理的语义知识。Context Archive 保存原始执行证据。Tool Journal 保存操作状态和实际结果。三者使用独立表和 API。

### 8.2 ArchiveRecord

    ArchiveRecord:
        archive_id
        scope_id
        sequence
        kind: raw | summary | prune | boundary | import
        payload
        payload_digest
        source_ids
        previous_id
        origin_run_id
        origin_session_id
        redaction_version
        created_at
        attachments

字段规则：

- raw：原始 LLMMessage、Tool Observation、用户输入或执行证据。
- summary：语义压缩结果，必须引用一个或多个 raw ArchiveRecord。
- prune：确定性裁剪记录，保存被裁剪消息的引用及裁剪原因。
- boundary：clear、rewind、new-session、fork 等上下文边界。
- import：从旧 Session 或其他 scope 导入的归档闭包。

archive_id 在 scope 内唯一。payload 使用 canonical JSON，计算 SHA256。ArchiveRecord 写入后不可原地修改；内容变化必须创建新记录并通过 previous_id 或 source_ids 关联。

### 8.3 Canonical Projection 与 Model View Projection

每个 scope 保存两套 Projection：

1. canonical：完整 Canonical History 的 Archive ID 有序列表。
2. model_view：当前模型上下文所引用的 raw、summary、prune 和 boundary 记录。

Compaction 只更新 model_view，不删除 canonical 记录。

Archive Projection：

    ArchiveProjection:
        scope_id
        projection_kind
        revision
        lineage
        messages_digest
        updated_at

messages_digest 校验当前投影展开后的消息序列。来源引用必须存在、归属相同 scope，且来源图不能形成循环。

### 8.4 ArchiveRewrite

压缩、裁剪、clear、rewind 和 import 统一使用显式 ArchiveRewrite：

    ArchiveRewrite:
        operation_id
        scope_id
        expected_revision
        kind
        expected_messages
        target_messages
        mapping
        derived_records
        reason

提交校验：

1. expected_revision 与当前 Projection Revision 一致。
2. mapping 长度与 target_messages 一致。
3. 保留消息必须与对应 raw source 内容一致。
4. 派生 summary/prune 必须声明 source_ids。
5. 所有 source_ids 必须存在且属于当前授权范围。
6. source graph 无循环。
7. 新 ArchiveRecord 的 payload_digest 正确。
8. 同一 archive_id 对应相同内容；同 ID 不同内容拒绝提交。
9. ArchiveRecord、Projection、CompactionCheckpoint 和 RunCheckpoint 在同一个 SQLite 事务中提交。

### 8.5 Compaction 集成

扩展 context/compaction.py：

- Stage A Tool Output Pruning：创建 prune ArchiveRecord，记录被裁剪的 event_ref 和原始 Archive ID。
- Stage B Structured Compaction：创建 summary ArchiveRecord，记录所有原始来源。
- ActiveCompactedView 持久化 active_summary_id、source_end_index、source_prefix_hash 和 checkpoint_id。
- CompactionCheckpoint 增加 source_archive_ids、summary_archive_id、projection_revision、policy_version、source_prefix_hash。
- restore_checkpoint_lineage 改为 restore_compacted_view，从 Store 读取 Summary 正文并验证哈希和来源闭包。
- Summary 来源不存在、哈希不匹配或 Projection 不一致时停止使用该 Summary，按 Canonical History 重建 Model Context。
- Compaction 的 semantic side-call Usage 与 ArchiveRewrite、Checkpoint 一起记录。
- Chat /clear 在创建 boundary record 后清空当前 Model View；历史 Archive 保留。
- clear_except_first、replace、rewind 等 History 变更必须经过 Archive-aware API，不能绕过 Projection 更新。

### 8.6 附件与大工具输出

大于 archive_inline_limit_bytes 的内容写入 Blob Store：

    blobs/sha256/<payload_digest>

Blob 写入：

1. 先写同目录临时文件。
2. flush + fsync。
3. 校验 SHA256。
4. chmod 0600。
5. os.replace。
6. fsync 父目录。
7. 在 SQLite 事务中提交 Blob 引用。

读取时检查文件为普通文件、路径在 Store 根目录内、无符号链接/重解析点，且摘要一致。缺失附件进入 recovery view，并关联对应 ArchiveRecord。

### 8.7 ArchiveRead Tool

新增 tools/archive_read.py，工具名称 archive_read。

参数：

    {
        "id": "archive id",
        "offset": 0,
        "limit": 4096
    }

合同：

- id 必须符合固定字符集和长度。
- offset >= 0。
- limit 为 1–16000 字符。
- scope_id 从当前 DurableExecutionContext 获取，模型不能传入任意 Session、Run 或文件路径。
- 返回 archive_id、kind、offset、total_chars、next_offset、has_more、content、payload_digest、source_ids。
- 内容先完成 digest 校验，再返回。
- 支持取消检查。
- 输出经过 ToolResult 和 Context Token Budget 限制。
- ToolEffect = READ_ONLY，Replay Policy = READ_ONLY。
- 历史文本作为低可信数据，不改变当前任务指令、Permission、Workspace Trust 或工具权限。

Summary 与 prune 标记必须把 archive_id 暴露给模型，并在 System Context 中说明可以使用 archive_read 读取完整原文。

## 9. Prefix Cache Audit

### 9.1 目标与接入位置

在每个 LLM Backend 构建实际 Provider Request 后、发送网络请求前，生成 RequestFingerprint。审计输入必须与最终 Provider 请求保持一致，不能只在 Agent 侧根据未序列化的 LLMMessage 猜测缓存前缀。

接入所有当前 Provider Backend 的最终请求构建位置，包括 OpenAI-compatible、Responses 和 Anthropic 等实际已注册 Backend。

### 9.2 RequestFingerprint

    RequestFingerprint:
        request_id
        run_id
        session_id
        step_id
        provider
        endpoint_identity
        model
        protocol
        component_hashes
        ordered_prefix_hashes
        full_request_digest
        created_at

Component 划分：

| 组件 | 具体内容 |
|---|---|
| system | System Prompt 和稳定 Runtime Instructions |
| tools | 最终序列化 Tool Schemas 及顺序 |
| repo_map | Repo Map 内容摘要 |
| skills | 已加载 Skill 与 Reference 内容摘要 |
| project_memory | 实际注入的 Project Memory 内容摘要 |
| history_prefix | 当前请求中较早且可复用的历史消息 |
| dynamic_tail | 最近消息、当前 Tool Observation 和动态注入提示 |
| request_options | model、protocol、reasoning、temperature、output limit 和 Provider 缓存选项 |

对每个组件使用 canonical serialization + SHA256。按 Provider 请求的实际顺序计算 cumulative prefix hash。与同一 cache_key 的上一条请求比较，记录第一个变化组件和后续变化组件。

cache_key 由 provider、endpoint_identity、model、protocol 和不包含密钥的账户标识组成。endpoint_identity 不包含 query secret、Authorization 或 API Key。

### 9.3 CacheAuditEvent

    CacheAuditEvent:
        request_id
        run_id
        session_id
        step_id
        provider
        model
        protocol
        prompt_tokens
        cached_tokens
        cache_write_tokens
        cache_read_reported
        output_tokens
        estimated
        cache_hit_ratio
        cache_miss_tokens
        component_hashes
        changed_components
        first_divergence
        previous_request_id
        idle_ms
        provider_cache_ttl_ms
        created_at

指标规则：

- cache_hit_ratio = cached_tokens / input_tokens，仅在 Provider 明确返回 cache read usage 且 input_tokens > 0 时计算。
- cache_miss_tokens = input_tokens - cached_tokens，仅在缓存用量可核实时计算。
- cache_read_reported=false 时，cache_hit_ratio 和 cache_miss_tokens 为 null，不将未知缓存用量当成 0。
- Prefix Hash 变化用于解释缓存前缀变化；Provider Usage 才是缓存命中 Token 的事实来源。
- provider_cache_ttl_ms 仅使用 Provider 明确文档或配置值；未知时为 null。
- 只保存哈希、组件名称、长度和 Usage，不保存完整 Prompt、Authorization、API Key 或敏感 Header。
- Cache Audit 写入失败只记录诊断，不改变 Agent 执行结果。

### 9.4 CLI 与报告

新增：

    forgeAgent cache report --run-id RUN_ID
    forgeAgent cache report --session-id SESSION_ID
    forgeAgent cache export --run-id RUN_ID --format json

报告展示：

- 每轮 input/cached/cache-write/output tokens。
- 缓存命中率及其可核实状态。
- 前缀变化组件。
- 首个变化位置。
- 与上一请求的空闲时间。
- 按 model/provider 汇总的缓存用量。
- estimated 和 unknown usage 统计。

## 10. First-run Onboarding

### 10.1 入口

新增：

    forgeAgent init
    forgeAgent doctor
    forgeAgent workspace trust
    forgeAgent workspace status

首次运行 forgeAgent run 或 forgeAgent chat 时，如果没有有效用户配置且当前为交互 TTY，启动 Onboarding。非交互环境输出明确配置要求，不等待交互输入。

init 支持：

    forgeAgent init
    forgeAgent init --provider PROVIDER --model MODEL
    forgeAgent init --provider PROVIDER --model MODEL --base-url URL
    forgeAgent init --non-interactive --provider PROVIDER --model MODEL --api-key-env ENV_NAME

--force 允许修改已有配置；取消引导时原配置保持不变。

### 10.2 Wizard 步骤

1. Provider / Protocol：从当前已注册 Provider 和协议能力中选择。
2. Model：选择模型名称、上下文窗口、最大输出 Token 和 reasoning 配置。
3. Endpoint：配置 Base URL、协议和连接参数。
4. Credentials：隐藏输入 API Key，选择系统 Keyring 或用户私有 env 文件。
5. Validation：实例化当前 Backend，执行最小验证请求，区分认证失败、网络失败、模型不可用和协议不匹配。
6. Workspace Trust：展示当前仓库身份、项目级 Skills 和项目配置来源，用户明确选择信任范围。
7. Environment Check：检查 Git、Python、pytest、Docker 和当前 Runtime 能力。
8. Summary：展示 Provider、Model、Config Path、Credential Source、Workspace Trust 和环境检查结果。

### 10.3 凭据存储

复用现有用户级配置：

- YAML：~/.config/forge-agent/config.yaml
- env：~/.config/forge-agent/env
- 环境变量：FORGE_ENV_FILE 可覆盖 env 文件位置。

保存规则：

- API Key 使用隐藏输入。
- 默认写入用户私有 env 文件，权限 0600；目录权限 0700。
- YAML 只保存环境变量引用，例如 api_key 使用 FORGE_LLM_API_KEY 环境变量引用。
- 更新 env 文件时保留其他键值，使用临时文件、fsync、replace 和父目录 fsync。
- 配置文件原有 Provider、models、MCP、Memory 和其他 Agent 设置必须保留。
- config show、doctor、Trace 和错误消息统一遮蔽凭据。
- 连接验证成功后才提交新配置；验证失败保留原配置并显示可操作诊断。
- Keyring 可用时允许用户选择 Keyring；Keyring 不可用时使用 env 文件。

### 10.4 WorkspaceTrust

WorkspaceTrust 按 Project Identity 记录，不只按当前绝对路径。

Trust Scope：

- project_skills：允许发现并加载仓库级 .agents/skills。
- project_guidance：允许加载仓库级 Agent 指令文件。
- project_mcp：允许读取仓库级 MCP 配置（该配置能力接入时启用）。
- project_hooks：允许加载仓库级 Hook 配置（该配置能力接入时启用）。

初始状态为 untrusted。首次发现项目级资源时显示来源和摘要，用户确认后写入 workspace_trust。

信任状态不改变 Tool Permission。所有实际工具调用仍然经过 ToolExecutor、PermissionManager、ExecutionConstraints 和 Workspace Boundary。Skill 继续作为知识按需加载，不自动执行 Skill 中的脚本。

提供：

    forgeAgent workspace trust --repo PATH
    forgeAgent workspace status --repo PATH
    forgeAgent workspace revoke --repo PATH --scope project_skills

信任记录包含 project_id、repo_identity、granted_scopes、trust_version、trusted_at 和 revoked_at。

### 10.5 Environment Doctor

doctor 检查：

- 当前配置文件路径与配置解析。
- Provider、Model、Protocol 和凭据引用。
- Backend 最小连接验证。
- Git 版本和仓库状态。
- Python 解释器和 pytest。
- Docker daemon、镜像和运行权限。
- Runtime Store 目录、SQLite WAL、权限和完整性。
- Skill 根目录和 MCP server 配置。
- Workspace Trust。
- 当前模型 Context Window、最大输出和 Token Budget 配置。

输出结构化 JSON 和可读摘要，支持 --json。

## 11. Skill / MCP Resource Lifecycle

### 11.1 Skill 安装清单

当前 skill add/list 继续保留。新增安装锁文件：

- Global：全局 Skills 根目录下的 skills.lock.json。
- Project：仓库 .agents/skills.lock.json。

每项记录：

    name
    scope
    source_type
    source_url
    git_ref
    resolved_commit_sha
    subpath
    installed_at
    content_digest
    manifest_digest
    skill_format_version

GitHub 安装时记录实际 checkout 的 commit SHA。Local 安装记录规范化源路径和内容摘要。安装完成后重新执行 Skill Catalog 校验。

### 11.2 Skill 命令

    forgeAgent skill add SOURCE [--local] [--repo PATH]
    forgeAgent skill list [--repo PATH]
    forgeAgent skill inspect NAME [--repo PATH]
    forgeAgent skill update NAME [--repo PATH]
    forgeAgent skill remove NAME [--local] [--repo PATH]

行为：

- add：安装一个通过 SKILL.md 校验的 Skill，写入 lock manifest。
- list：显示名称、来源、scope、版本、是否被当前 WorkspaceTrust 允许。
- inspect：显示完整 metadata、资源目录、来源 Commit 和内容摘要。
- update：按 lock 中的来源重新获取资源，展示 Commit 和 content_digest 变化，成功后原子更新目录和 lock。
- remove：只允许删除对应 Skill 根目录内的目标目录，并同步 lock manifest。
- 更新或移除时使用临时目录和原子替换；失败时保留旧 Skill。

### 11.3 MCP 命令

当前 mcp add/list 继续保留。新增：

    forgeAgent mcp add ID --url URL
    forgeAgent mcp add ID --command COMMAND [--arg ARG ...]
    forgeAgent mcp list
    forgeAgent mcp inspect ID
    forgeAgent mcp test ID
    forgeAgent mcp enable ID
    forgeAgent mcp disable ID
    forgeAgent mcp remove ID

行为：

- inspect：显示 transport、command、args、endpoint 和脱敏 env 引用。
- test：启动目标 Server，执行 initialize、capability discovery、list_tools 和 close；输出工具数量、schema 校验结果、server capability 和耗时，不调用远端业务工具。
- enable/disable：只更新目标 server 的 enabled 字段。
- remove：移除目标 server 配置，保留其他 YAML 字段、注释和顺序。
- add/update/remove 使用现有 YAML 安全写入方式，验证修改前后其他配置值不变。
- server.env 只接受显式环境变量引用；config show 和错误消息不显示实际值。
- MCP Server 的 Tool Effect 和 Replay Policy 分开配置；未声明 Replay Policy 的远端工具默认为 UNKNOWN。

## 12. CLI、Chat 与 API 交互

### 12.1 Runtime 命令

新增命令组：

    forgeAgent runtime status [--repo PATH]
    forgeAgent runtime runs [--repo PATH] [--status STATUS]
    forgeAgent runtime inspect RUN_ID
    forgeAgent runtime resume RUN_ID
    forgeAgent runtime pause RUN_ID
    forgeAgent runtime recover list [--run-id RUN_ID]
    forgeAgent runtime recover inspect OPERATION_ID
    forgeAgent runtime recover resolve OPERATION_ID --decision DECISION --reason TEXT
    forgeAgent runtime recover retry OPERATION_ID --reason TEXT
    forgeAgent runtime backup [--repo PATH]
    forgeAgent runtime verify [--repo PATH]

resume 必须校验 Repo Identity、Worktree 路径、Config Digest、Tool Registry Digest 和未决 Operation。模型切换通过显式 --model 覆盖记录，并写入新的 resume decision。

pause 在当前安全边界完成当前 ToolResult/Observation 后保存 Checkpoint。运行中的同步 Tool 不被强制杀死；取消请求在现有 Cooperative Cancel 边界生效。

### 12.2 Chat 命令

新增 Chat 内置命令：

- /resume：显示当前 Session 的中断 Run，并恢复指定 Run。
- /recovery：显示未决 Operation 和恢复状态。
- /archive：查看当前 Session 的 Archive 摘要和可读取记录。
- /cache：显示当前 Session 的 Cache Audit 摘要。
- /clear：先提交 Archive boundary，再清空当前 Model Context。

Chat 的 PendingRoundState 增加 run_id、scope_id 和 runtime_revision。旧版本 Session JSON 自动补齐空值并兼容加载。

### 12.3 API

API 继续由 ApiTaskStore 管理 HTTP 队列和任务状态，由 TaskEngine 管理 isolate 任务认领和 Worktree 状态，由 DurableRuntimeStore 管理 Agent Run、Tool Operation、Checkpoint 和 Recovery。

新增或扩展 API：

- 查询 Run 的 Durable 状态。
- 查询 Run Checkpoint 和最近事件。
- 请求暂停或取消。
- 请求恢复已中断 Run。
- 查询 outcome_unknown Operation。
- 提交 Recovery Decision。

API 返回 Agent Status、Acceptance Status、Delivery Status 和 Durable Lifecycle Status 四种独立字段。

## 13. ExecutionRunner 与模块集成合同

### 13.1 RunRequest

扩展 agent.runner.RunRequest：

    run_id
    scope_id
    durable_store
    resume_checkpoint_id
    resume_decision
    parent_run_id

RunRequest 仍负责描述本次执行参数。DurableRuntimeContext 负责提供 Store、Owner Receipt、Run Identity、Archive Reader 和 Operation Journal。

### 13.2 ExecutionRunner

ExecutionRunner.run 的统一流程：

1. 解析 Project Identity。
2. 打开 DurableRuntimeStore。
3. 创建或接管 Scope Owner。
4. 创建 RunRecord 或加载指定 RunRecord。
5. 校验 Config、Tool Registry、Repo 和 Worktree 身份。
6. 创建 Agent/Orchestrator。
7. 将 DurableRuntimeContext 注入 Agent 和 ToolExecutor。
8. 执行 Agent。
9. 保存 RunResult。
10. 执行 Independent Acceptance 并保存结果。
11. 如请求 GitHub Delivery，使用同一 Operation Journal 管理 commit/push/create PR。
12. 保存 Delivery Result 和最终 Run Lifecycle。
13. 关闭外部 MCP、Runtime 和 Store 资源。

### 13.3 Agent Core

agent/core.py：

- 每次模型请求创建 model_call 记录。
- 响应返回后保存 Action 和 Usage。
- ToolCall 交给 ToolExecutor，传入 logical_tool_call_id、action_event_id 和 Durable Journal。
- ToolResult 由 Journal 提交，Observation 追加到 History 后提交 RunCheckpoint。
- Planning/Recovery/Skill Runtime 状态在每个 control action 后写入 Checkpoint。
- FINISH Guard 的全部状态进入 Checkpoint。
- ArchiveRead 使用当前 scope 的 ArchiveReader。

### 13.4 ToolExecutor

harness/executor.py：

- 增加 ToolExecutionJournal Protocol。
- 增加 approval receipt 和 policy digest。
- 在 invocation_callback 前提交 started。
- ToolResult 返回后提交 result + observation。
- 已有 result_committed 时直接返回已保存结果，不再次调用 Tool。
- outcome_unknown 时返回专用恢复状态并阻止副作用执行。
- Journal 错误映射为明确的 durable storage infrastructure failure，停止当前 Run。

### 13.5 EventLog

EventLog 继续记录 Trace v2，并增加：

- RUN_CHECKPOINT_COMMITTED
- TOOL_INTENT_COMMITTED
- TOOL_OPERATION_STARTED
- TOOL_RESULT_COMMITTED
- TOOL_OUTCOME_UNKNOWN
- TOOL_RECOVERY_DECIDED
- ARCHIVE_RECORD_COMMITTED
- ARCHIVE_REWRITE_COMMITTED
- ARCHIVE_READ
- CACHE_AUDIT_RECORDED
- WORKSPACE_TRUST_CHANGED
- ONBOARDING_COMPLETED

每条事件关联 run_id、scope_id、operation_id/archive_id/request_id、revision 和 owner_epoch。EventLog 的写入结果不改变 Durable Store 的事务结果。

### 13.6 Chat Session 迁移

JsonChatSessionStore 继续保留 Chat Session 元数据、标题、轮次列表、统计和兼容读取。

活动 Run 的中间 History、pending Action、Tool Operation、Archive Projection 和 Runtime Snapshot 由 DurableRuntimeStore 管理。

ChatSessionState.history 是最近一次完整 round 的会话视图快照。PendingRoundState.run_id 指向当前活动 Run。Chat 恢复时：

- 没有 pending_round：使用 ChatSessionState.history。
- 有 pending_round 且 Run 存在：加载 Durable RunCheckpoint 和 Archive Projection。
- 有 pending_round 但 Run 不存在：按旧 Session 恢复路径处理，并写入 migration diagnostic。
- 旧 Session 中只有 compaction checkpoint metadata、没有 Summary 正文时，保留旧记录并将 legacy_gap=true；从 Canonical History 重建新的 Model View。

Session JSON Version 升级必须支持 v1/v2 到新版本的兼容加载。迁移成功后才写入新版本。

### 13.7 Existing Store 的边界

- TaskEngine 继续管理 isolate DAG、任务 claim 和依赖状态。
- ApiTaskStore 继续管理 HTTP 请求生命周期。
- JsonChatSessionStore 继续管理 Chat Session 元数据和兼容快照。
- PersistentRepoIndex 继续管理 RepoMap 结构索引。
- Project Memory 继续管理语义知识。
- EventLog 继续管理 Trace。
- DurableRuntimeStore 管理 Agent Run 的执行事实、操作结果、Checkpoint、Archive 和 Cache Audit。

所有存储通过 run_id、session_id、task_id、project_id 和 event_ref 关联，不把各自状态表合并成一个无边界的总表。

## 14. 配置设计

config/schema.py 增加：

    DurableRuntimeConfig:
        enabled: bool = true
        root: str | null = null
        busy_timeout_ms: int = 5000
        checkpoint_every_steps: int = 1
        archive_enabled: bool = true
        archive_inline_limit_bytes: int = 65536
        archive_read_max_chars: int = 16000
        backup_on_migration: bool = true

    CacheAuditConfig:
        enabled: bool = true
        retention_days: int = 90
        store_component_hashes: bool = true

    OnboardingConfig:
        enabled: bool = true
        auto_start: bool = true
        workspace_trust_required: bool = true
        credential_store: str = "env_file"

AppConfig 增加 durable_runtime、cache_audit、onboarding 三个配置段。

配置解析必须校验：

- root 为空时使用默认用户目录；非空时展开并要求绝对路径。
- busy_timeout_ms 为正整数。
- checkpoint_every_steps 为正整数。
- archive_inline_limit_bytes、archive_read_max_chars、retention_days 为正整数。
- credential_store 仅接受当前实现支持的 keyring/env_file。
- 未知配置键报错，不静默忽略。
- config show 对密钥、env value、凭据引用进行遮蔽。

默认配置：

    durable_runtime:
      enabled: true
      busy_timeout_ms: 5000
      checkpoint_every_steps: 1
      archive_enabled: true
      archive_inline_limit_bytes: 65536
      archive_read_max_chars: 16000
      backup_on_migration: true

    cache_audit:
      enabled: true
      retention_days: 90
      store_component_hashes: true

    onboarding:
      enabled: true
      auto_start: true
      workspace_trust_required: true
      credential_store: env_file

## 15. 安全与数据治理

### 15.1 凭据

- Trace、Archive、Cache Audit、Recovery Decision 和普通日志均执行字段级及文本级 redaction。
- 凭据使用环境变量名或 Keyring Reference。
- Tool Intent 保存参数摘要和安全载荷引用；可检测的敏感值不写入明文审计字段。
- 对不可安全恢复的敏感参数设置 replayable=false。
- 用户级 env 文件和 Store 文件使用私有权限。
- config show、CLI 错误、doctor 和运行报告不输出真实凭据。

### 15.2 Archive 数据

- Archive 记录不可变。
- ArchiveRead 只读取当前 scope。
- 所有来源引用校验 scope、digest 和 lineage。
- 原始消息中的敏感字段在持久化前执行 redaction。
- 大文件使用内容寻址 Blob，并检查路径、文件类型、链接数和 digest。
- Archive 内容是历史数据，不改变当前 Permission 和 Workspace Trust。

### 15.3 Workspace 与 Tool 权限

- Project Skills、Project Guidance 和 Project MCP 配置按 WorkspaceTrust scope 授权。
- WorkspaceTrust 不自动授权 Shell、文件写入、Git push 或 MCP 副作用。
- ToolExecutor 仍是所有工具执行的唯一治理入口。
- Permission、ExecutionConstraints 和 Replay Policy 分别负责授权、执行边界和重放语义。

### 15.4 Store 故障

- Intent 写入失败：不执行 Tool。
- Started 写入失败：不执行 Tool。
- Result 写入失败：停止 Run，保留 started 状态并进入恢复。
- Checkpoint 写入失败：不继续发起下一次模型请求。
- Archive Rewrite 写入失败：不发布新 Model View。
- Cache Audit 写入失败：记录 telemetry warning，继续执行。
- 数据库损坏或 Schema 不兼容：保留原数据库，停止自动恢复并输出备份/诊断路径。

## 16. 测试设计

### 16.1 Durable Store

新增 tests/test_durable_runtime_store.py：

- SQLite WAL、synchronous、foreign_keys 和 busy_timeout 实际配置核验。
- Schema 创建、版本迁移、旧版本拒绝和损坏数据库处理。
- Revision CAS、Owner Epoch 和事务回滚。
- 同一 scope 双进程 claim 冲突。
- 原 Owner 退出后的新 Owner 接管。
- 旧 Owner 写入被 fencing。
- 不同 scope 并发写入。
- 进程中断后 started Operation 自动变成 outcome_unknown。
- Store 备份和 integrity_check。

### 16.2 Run Checkpoint

新增 tests/test_durable_run_checkpoint.py：

- 模型 Action 返回后恢复 pending_action。
- response_committed 后不再次调用模型。
- model_call started 中断后记录 unknown usage。
- PlanningRuntime/RecoveryRuntime/SkillRuntime 状态 round-trip。
- LoopDetector、SemanticProgressTracker 和 Completion Guard 状态 round-trip。
- Canonical History 保留 event_ref、tool_call_id 和 responses_call。
- Repository/Worktree/Config/Tool Registry 身份不匹配时拒绝恢复。
- Chat pending_round 能通过 run_id 找到 Durable Checkpoint。
- Chat v1/v2 Session 兼容迁移。

### 16.3 Tool Journal 与故障注入

新增 tests/test_durable_tool_journal.py 和 tests/test_durable_tool_recovery.py。

必须注入以下中断点：

| 中断位置 | 恢复预期 |
|---|---|
| Intent Commit 前 | Tool 未执行 |
| Intent Commit 后、Started 前 | 状态为 intent_committed |
| Started Commit 后、Tool 调用前 | 接管后 outcome_unknown |
| Tool 执行过程中 | 接管后 outcome_unknown |
| Tool 执行完成、Result Commit 前 | outcome_unknown，不自动重试 |
| Result Commit 后、Observation 入 History 前 | 从已提交 Observation 恢复，不再执行 Tool |
| Observation 入 History 后、Checkpoint Commit 前 | 根据 ToolRecord 补齐检查点 |
| Checkpoint Commit 后 | 从下一 step 继续 |
| Result Commit 事务失败 | Run 停止，操作保持待核实 |
| 同一 operation_id 参数变化 | 拒绝提交 |
| 同一 decision_id 内容变化 | 拒绝提交 |
| 未决副作用存在时尝试 mutation | 阻断 |
| 未决副作用存在时执行 READ_ONLY inspect | 允许 |

对 file_write、file_edit、git commit、git push、GitHub PR 和 MCP 分别验证对应 Reconciler。

核心断言：结果未知的非幂等副作用不会被系统自动重复执行。

### 16.4 Archive 与 Compaction

新增 tests/test_context_archive.py、tests/test_archive_read.py 和 tests/test_archive_compaction_integration.py：

- raw ArchiveRecord 写入和 digest 校验。
- summary/prune 来源闭包校验。
- 同 ID 不同内容冲突。
- 来源缺失、循环、跨 scope 引用和 digest 错误拒绝。
- Canonical Projection 与 Model View Projection 独立更新。
- Compaction 先提交 Archive，再发布 history_override。
- Stage A prune 产生来源记录。
- Summary 跨进程恢复。
- ArchiveRead offset/limit 边界、取消和 scope 隔离。
- 超大输出 Blob 原子写入、缺失附件和内容篡改。
- clear/new session/rewind 产生 boundary。
- Responses function_call/function_call_output 配对完整性。

### 16.5 Prefix Cache Audit

新增 tests/test_prefix_cache_audit.py：

- 相同请求得到相同 full_request_digest。
- System、Tool Schema、RepoMap、Skill、Project Memory 和 History 各自变化时定位正确组件。
- 动态 Tail 变化不会误报为稳定前缀。
- Model、Protocol、Endpoint 变化切换 cache_key。
- Provider Usage 返回 cached_tokens 时命中率计算正确。
- cache_read_reported=false 时指标为 null。
- estimated Usage 不被当作 Provider 真实缓存命中。
- API Key、Authorization 和完整 Prompt 不进入 Audit。
- Audit Store 故障不影响 Agent 请求。

### 16.6 Onboarding 与资源管理

新增 tests/test_onboarding.py、tests/test_workspace_trust.py、tests/test_resource_lifecycle.py：

- 首次交互 TTY 启动向导。
- 非交互环境不阻塞。
- 用户取消时原配置不变。
- 无效凭据不写入有效配置。
- env 文件保留已有键、权限正确、原子替换。
- config.yaml 保留原有模型、MCP、Memory 和 Agent 配置。
- config show、doctor 和异常不泄露凭据。
- WorkspaceTrust 按 scope 授权和撤销。
- 未信任 Project Skill 不加载。
- Global Skill 不受 Project Trust 影响。
- Skill add/update/remove 和 lock manifest 一致。
- GitHub Skill 记录 resolved_commit_sha。
- MCP test 完成连接、发现和清理，不调用业务工具。
- MCP enable/disable/remove 不改变其他 YAML 值。

### 16.7 端到端恢复测试

新增 tests/test_long_horizon_reliability.py，使用 MockBackend、Fake Tool、Fake MCP Server、临时 SQLite 和多进程故障注入，覆盖：

1. 一个 Run 连续执行多个模型请求和工具调用，期间进行多次 Compaction。
2. 在不同 Tool Lifecycle 边界杀死进程并恢复。
3. 从 RunCheckpoint 恢复 Planning、Recovery、Skills、History 和 Usage。
4. ArchiveRead 找回早期任务约束和原始工具证据。
5. GitHub Delivery 在 push/PR 响应未知时通过 Reconciler 恢复。
6. 多个独立 Run 并发执行。
7. 同一 Chat Session 的并发 Owner 冲突。
8. 旧 Owner 恢复写入被拒绝。

## 17. Long-horizon Reliability Benchmark

在 Evaluation Harness 中新增 Long-horizon Reliability Suite。所有场景使用冻结 Task、Tool Contract、Failure Injection Point 和 Verifier。

### 17.1 场景分组

| 分组 | 场景 |
|---|---|
| Run Recovery | LLM response committed、Action pending、Checkpoint restore |
| Tool Outcome | Intent、Started、Result 各阶段崩溃 |
| Side-effect Safety | Git push、PR create、MCP 副作用、文件写入 |
| Context Recall | 多次 Compaction 后读取早期约束和原文 |
| Archive Integrity | 缺失来源、哈希损坏、附件丢失 |
| Cache Audit | 稳定前缀、单组件变化、多组件变化、未知 Provider Usage |
| Ownership | 多进程 claim、旧 Owner fencing、不同 scope 并发 |
| Onboarding | 新用户、错误凭据、取消、Workspace Trust |

### 17.2 核心指标

| 指标 | 定义 | 目标 |
|---|---|---|
| Duplicate Side-effect Execution Count | 同一逻辑操作被重复执行的次数 | 0 |
| Unknown Outcome Detection Rate | 已进入 Started 且无 Result 的操作被识别为 Unknown 的比例 | 100% |
| Result Replay Accuracy | result_committed 后恢复的 ToolResult 与原结果一致比例 | 100% |
| Checkpoint Recovery Success Rate | 可恢复检查点成功还原 Runtime State 的比例 | 100% |
| Context Archive Recall Accuracy | ArchiveRead 返回内容与原始归档一致的比例 | 100% |
| Archive Integrity Detection | 对被篡改或缺失记录的检测比例 | 100% |
| Owner Fencing Accuracy | 旧 Owner 写入被拒绝的比例 | 100% |
| Prefix Divergence Accuracy | 变化组件与冻结 Provider Request 对比一致的比例 | 100% |
| Cache Usage Attribution | Provider 返回 Usage 与 Audit 记录一致比例 | 100% |
| Onboarding Persistence Accuracy | 配置写入、取消回滚和凭据遮蔽符合合同的比例 | 100% |

### 17.3 评测输出

每个 Trial 保存：

- task_id、run_id、project_id 和 scope_id。
- Git Commit、Repo Snapshot、Config Digest、Tool Registry Digest。
- Failure Injection Point。
- Owner Epoch 和 Revision。
- 操作状态转换。
- Recovery Decision 和证据。
- Archive Record/Projection Digest。
- Cache Audit Event。
- Agent Status、Acceptance Status、Delivery Status。
- Verifier 结果和完整 Trace 引用。

Benchmark 不使用跨 Trial 的 Session History、Archive Projection、Cache Comparator 或 Project Memory。模型效果评测与确定性故障注入分开报告。

## 18. 实施顺序

Codex 按以下依赖顺序完成全部实现。每个阶段完成代码、测试、文档、CLI 和配置接线后进入下一阶段。

| 阶段 | 实施内容 | 交付结果 |
|---|---|---|
| P3-0 | Durable Runtime Contracts、SQLite Store、Schema、Owner Epoch、Revision、备份和迁移 | 可用的持久化状态层 |
| P3-1 | RunRecord、RunCheckpoint、Model Call Journal、ExecutionRunner 接线、Chat Session run_id 关联 | 跨进程恢复 Run 和模型动作 |
| P3-2 | Tool Intent/Started/Result、ToolExecutor 接线、Replay Policy、Result/Observation 恢复 | 持久化工具执行生命周期 |
| P3-3 | Unknown Outcome、Reconciler、Recovery Decision、CLI runtime recover、GitHub Delivery Journal | 副作用安全恢复 |
| P3-4 | Raw Context Archive、Archive Projection、ArchiveRewrite、Blob Store、ArchiveRead | 可追溯的长期上下文 |
| P3-5 | TraceableCompaction 持久化、Chat clear boundary、旧 Session 迁移 | 跨进程恢复压缩上下文 |
| P3-6 | Provider RequestFingerprint、Cache Audit、Usage 对账和 CLI 报告 | 缓存变化可诊断 |
| P3-7 | First-run Onboarding、WorkspaceTrust、Doctor、凭据存储 | 首次启动配置闭环 |
| P3-8 | Skill/MCP Lock Manifest、inspect/update/remove/test/enable/disable | 扩展资源生命周期管理 |
| P3-9 | Long-horizon Reliability Suite、全入口回归、文档和 Usage 收口 | 完整验收和可复现报告 |

阶段之间允许复用前一阶段的公共接口；最终交付必须包含所有阶段，不以单独的 Store 或 Journal 完成作为本设计完成。

## 19. 完成验收标准

以下条件全部满足时，本设计完成：

1. CLI run、Chat、API、GitHub Issue 和 Evaluation 都通过 ExecutionRunner 使用 DurableRuntimeStore。
2. 每个 Run 有稳定 run_id、scope_id、Owner Epoch、Revision 和可恢复 Checkpoint。
3. 模型响应解析后的 Action 在工具执行前持久化。
4. Tool Intent 在实际执行前提交，Tool Result 在 Observation 返回前提交。
5. Result 已提交的 ToolCall 恢复时不再次执行。
6. Started 无 Result 的操作恢复为 outcome_unknown。
7. outcome_unknown 的非幂等操作不会被自动重试。
8. Reconciler 和 Recovery Decision 可以确认操作状态，并保留证据。
9. GitHub commit/push/create PR 进入 Operation Journal。
10. Canonical History、Model View 和 Context Archive 分离。
11. 每个 Summary 和 Prune 都能追溯到原始 ArchiveRecord。
12. ArchiveRead 只能读取当前授权 scope，并通过 digest 校验。
13. Compaction Archive Rewrite 和 Projection 更新具备原子提交语义。
14. Chat 进程重启后能恢复 Run、History、Planning、Recovery、Skills、Usage 和 Compaction View。
15. Prefix Cache Audit 使用最终 Provider Request 生成指纹，并与 Provider Usage 对账。
16. Onboarding 完成 Provider、Model、Credential、WorkspaceTrust 和环境检查。
17. Skill/MCP 安装清单保存来源和版本，资源生命周期命令可用。
18. 多进程、崩溃恢复、数据损坏和敏感信息测试全部通过。
19. Long-horizon Reliability Benchmark 输出完整 Trial、Failure Injection、Verifier 和指标报告。
20. README、USAGE、docs/design、源码索引、architecture-map 和测试索引同步更新。

## 20. 主要源码落点

新增模块建议：

| 路径 | 职责 |
|---|---|
| durable_runtime/contracts.py | Run、Owner、Intent、Operation、Checkpoint、Recovery 数据合同 |
| durable_runtime/store.py | SQLite WAL、Schema、事务、Revision、Owner Epoch |
| durable_runtime/ownership.py | ProcessIdentity、Scope Claim、Owner Fencing |
| durable_runtime/run_manager.py | Run 创建、恢复、暂停、终止 |
| durable_runtime/checkpoint.py | Runtime Snapshot、Checkpoint Commit/Restore |
| durable_runtime/operation_journal.py | Intent、Started、Result、Recovery Decision |
| durable_runtime/reconcilers.py | 文件、Git、GitHub、MCP Reconciler 接口和实现 |
| context/archive.py | ArchiveRecord、Projection、ArchiveRewrite、Blob Store |
| tools/archive_read.py | scope-bound ArchiveRead Tool |
| llm/cache_audit.py | RequestFingerprint、CacheAuditSink、Report |
| entry/onboarding.py | First-run Wizard、Credential Setup、WorkspaceTrust |
| entry/doctor.py | 环境与配置诊断 |
| evals/long_horizon/ | 故障注入 Suite、Verifier、Report |

主要修改模块：

- agent/core.py
- agent/runner.py
- agent/session.py
- agent/session_store.py
- agent/event_log.py
- agent/planning.py
- agent/recovery.py
- harness/executor.py
- tools/base.py
- context/history.py
- context/compaction.py
- context/structured_compaction.py
- llm/base.py
- llm/usage.py
- llm 各 Provider Backend 的最终 Request 构建位置
- project_memory/identity.py
- entry/chat.py
- entry/cli.py
- entry/resource_commands.py
- entry/context_common.py
- config/schema.py
- config/default.yaml
- docs/design/源码索引.md
- docs/design/architecture-map.json
- docs/README.md
- USAGE.md

Codex 实施前必须重新核对 dev 最新 HEAD 和上述文件的实际符号；实施中以本文数据合同为目标，结合当前源码调整具体函数签名。新增文件和公共数据结构必须加入测试、源码索引和架构映射。

## 21. 参考实现

- Forge Agent：Napabana/forge-agent，dev，基线 f6f5b5616f3b287dd4286d7377b116582948b7af。
- RuoCun：hmmgotit/RuoCun，main，基线 2e7eaa3a7d76c11a437c4e5c4ca26443ffa851f4。
- RuoCun Durable Store：src/ruocun/session/contract.py、src/ruocun/session/durable_store.py、src/ruocun/session/recovery.py。
- RuoCun Context Archive：src/ruocun/session/archive.py、src/ruocun/tools/archive_read.py。
- RuoCun Cache Audit：src/ruocun/session/cache_audit.py、src/ruocun/agent/context_runtime.py。
- RuoCun Onboarding：src/ruocun/tui/onboarding/。
- Forge 现有执行合同：docs/design/01-任务执行主链.md、docs/design/02-Agent与Tool生命周期.md、docs/design/03-Context与RepoMap.md、docs/design/04-Planning与Recovery.md、docs/design/05-Skills与MCP.md、docs/design/06-隔离与独立验收.md、docs/design/10-Project-Memory-架构.md。
