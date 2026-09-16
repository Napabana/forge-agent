# Forge Agent P0/P1 当前状态

> 执行原则：一次只迁移一个契约；先固定基线和验收，再改生产代码；每个功能必须经过统一 Harness，并留下源码、测试、Trace 或实验报告证据。

## 0. 开工门禁

- [ ] 读取 `AGENTS.md`，检查 `git status --short --branch`、最近提交、remote 和 stash。
- [ ] 保留 `config/default.yaml` 的用户修改，不提交、不还原。
- [ ] 核对 Forge 当前分支为 `dev`，并记录基线 Commit。
- [ ] 建立可运行的 Python 3.11+ 测试环境；Windows `.venv` 若仍指向失效解释器，重新创建或使用独立 WSL venv。
- [ ] 先跑 `tests/test_day2.py`、`tests/test_harness.py`、`tests/test_chat.py`、`tests/test_orchestrate.py` 的基线，记录真实 passed/failed/skipped 和耗时。
- [ ] 在不调用真实模型的情况下跑完核心 Harness 测试。
- [ ] 记录 Pi 基线 Commit `71dca871bc80b6bc97be37f0ca3189399d651fff`，保留其 `google-shared.ts` 用户修改。
- [ ] 阅读 Pi 的 `types.ts`、`agent-loop.ts`、对应 tests 和 Coding Agent 产品层接线，再形成 Forge 设计说明。
- [ ] 对 DeepSeek Harness、Codex、Claude Code 分别确认“可审源码/仅官方契约/不可验证”，未确认的实现细节标记为待确认。
- [ ] 保存面试材料来源：`E:\2806\简历\面试项目复盘\outputs\项目-forge-agent.html`，建立 23 道问题到代码、测试、实验或表达复练的映射。
- [ ] 建立高风险 Claim 账本：Ownership、Metric、Architecture、Result；每项标记已验证、部分验证、待实现或应降低表述。
- [ ] 选定一个可复现真实 Issue 作为贯穿 P0/P1 的 case study，写清原始验收条件且在运行中不可由 Agent 自行修改。

## P0-1：`prepare_next_turn` 最小策略插槽

### 设计

- [x] 在 `agent/core.py` 定义最小类型：`PrepareNextTurnContext`、callback 类型和必要结果类型。
- [x] 决定 callback 由 `AgentConfig` 还是 `Agent.__init__` 注入；优先选择组合根容易配置、测试容易替换、不会把产品策略写进 Task 的方式。
- [x] 沿用 Pi 的 turn 边界：第一轮不执行；一个 turn 的工具结果进入 History 后、下一次 `_build_messages()` 前执行。
- [x] 明确 callback 返回 `None` 时保持当前状态。
- [x] 第一版保持同步 callback 和同步 `Agent.run()`。
- [x] 不在 context 中暴露完整 Agent；只暴露 task、step、history、repo_map、token_budget、cancel_event 等必要依赖。
- [x] callback 前后检查取消。
- [x] callback 异常转为结构化 RunResult 失败，并保留原异常分类；不得静默继续。
- [x] FINISH、GIVE_UP、已取消、最大步数结束后不得多执行 callback。
- [x] 本 diff 不实现 compaction、不重构 History、不重构 Repo Map、不修改 TokenBudget 算法。

### 测试

- [x] 默认 `None` 时，MockBackend 收到的消息和既有行为一致。
- [x] 两轮工具任务中 callback 恰好在第二次 LLM 调用前执行一次。
- [x] callback 注入状态后，下一次 backend 可以看到。
- [x] callback 返回 `None` 不改变下一轮消息。
- [x] callback 抛异常后不再调用 LLM，任务产生明确失败结果和事件。
- [x] callback 前已经取消时不执行 callback 和 LLM。
- [x] callback 执行期间触发取消时，callback 后不调用 LLM。
- [ ] callback 不会导致共享 Chat History 重复插入用户消息。
- [x] callback 请求 Repo Map 刷新后，下一轮使用刷新结果；结果类型使用显式 `refresh_repo_map`。
- [ ] 增加与 Pi `prepareNextTurn snapshot` 对照的契约测试说明，但不复制 TypeScript 测试代码。

### 验收

- [x] `Agent.run()` 主循环新增逻辑保持为少量、单一职责的策略调用。
- [x] 未配置 callback 的相关定向回归通过。
- [x] 所有新增失败和取消路径有测试。
- [x] README/设计文档明确第一轮不调用和同步边界。

## P0-2：Tool Hook 生产语义与接线

### 契约

- [x] 固定执行顺序：Pre Hook -> Permission -> Tool -> Post Hook -> Observation。
- [x] 在 Pre Hook 前完成统一 Tool Schema 参数校验；未知工具和非法参数转换为稳定错误结果，不执行底层工具。
- [x] Pre Hook 返回类型化 block 结果，不再依赖任意非 `None` 值的隐式语义；兼容策略需写测试。
- [x] 第一版 Post Hook 只观察，不改写 ToolResult。
- [x] 明确 Pre Hook 异常默认 fail-closed，避免安全检查异常时放行。
- [x] 明确 Post Hook/纯观察者异常默认 fail-open，但必须记录 Trace。
- [x] 权限判断与 Hook 不是同一层：Hook 负责扩展策略，PermissionManager 负责安全决策。
- [ ] 如果需要结果脱敏或截断，新增显式 `ObservationTransform` 设计，不借 Post Hook 隐式返回值完成。
- [ ] 记录 Pi 支持 after-hook 字段覆盖的事实，以及 Forge 第一版暂不采用的理由。
- [ ] 定义稳定错误分类：`unknown_tool`、`invalid_arguments`、`permission_denied`、`timeout`、`tool_execution`、`infrastructure`、`provider`、`retry_exhausted`。
- [x] 确认参数校验使用现有 schema 的最小实现还是引入依赖；没有充分收益证据前优先复用现有类型和标准库。

### 生产接线

- [x] 组合根能够注入 Hooks；`orchestrate_run()` 不再永远构造无 Hooks 的 ToolExecutor。
- [ ] 普通 Run、Isolated Run、Chat、GitHub Issue/自动 PR 使用同一 ToolExecutor 工厂或 Runner 配置。
- [x] Hooks 不直接 import entry/UI 层。
- [x] 同一个执行请求中的 Hooks 实例所有权和生命周期明确，不使用全局可变注册表。

### 测试

- [ ] Pre block 阻止底层工具执行并返回结构化错误 Observation。
- [x] 非法参数在 Pre Hook 和 Tool 执行前被拒绝，且错误类别可断言。
- [x] 未知工具、Permission deny、Tool exception、timeout 分别落到不同错误类别。
- [x] Pre Hook 异常不执行工具，并记录失败原因。
- [ ] Permission deny/confirm/allow 顺序正确。
- [x] Post Hook 能看到 raw ToolResult。
- [x] Post Hook 返回值不会改变 ToolResult。
- [x] Post Hook 异常不破坏已完成的工具结果，但 Trace 中可见。
- [x] 多个 Hook 的注册顺序和短路语义确定。
- [ ] 每个产品入口至少一个集成测试证明 Hook 真正触发。

## P0-3：Trace v2 最小闭环

### Schema

- [x] 定义 `run_id/session_id/turn_id/step_id/span_id/parent_span_id` 的生成和传播规则。
- [x] 增加 `prepare_next_turn_started/finished/failed` 事件。
- [x] 增加或扩展 LLM span：模型、provider、重试、input/output token、duration。
- [x] 增加 Tool span：工具名、duration、结果字节数、成功/错误分类。
- [ ] 增加 Prompt 分区统计：system、tool schema、Repo Map、history、observation；区分 provider usage 与本地估算。
- [ ] 记录 Hook 与 Permission 决策，但不泄露秘密。
- [ ] 记录取消发生在 prepare/LLM/tool/turn 边界中的哪个阶段。
- [x] 为事件 schema 增加版本号，旧 JSONL 仍可读取或明确迁移边界。

### 敏感信息

- [ ] 禁止默认记录 API Key、Authorization、Cookie、完整环境变量。
- [ ] 对命令参数和工具结果定义 allowlist/redaction/truncation 策略。
- [ ] raw model response 是否持久化必须可配置；默认只保留必要诊断字段。
- [ ] 增加秘密脱敏测试。

### 测试与报告

- [ ] 同一 Run 的父子 span 可以重建顺序。
- [x] callback、LLM 和 tool 的 duration 非负且字段完整。
- [x] MockBackend 的 input/output token 进入对应 turn 和 run 汇总。
- [x] observer/trace writer 失败不能覆盖原始 Agent 结果，但必须产生可诊断警告。
- [x] 编写最小 JSONL 汇总器，输出 actions、tool calls、tokens、duration、final status。
- [ ] 汇总器输出各错误类别发生次数、恢复成功次数、false-finish 和 cancel latency。

## P1-1：统一 `ExecutionRunner`

### 领域接口

- [x] 定义 `RunRequest`：task、repo、history/session、isolation、sandbox、hooks、permission、cancel、result policy。
- [x] 定义 `RunArtifact` 或复用并扩展 `RunResult`：patch、worktree、branch、tests、trace path、delivery status。
- [x] 定义独立 `AcceptanceContract` 第一版：支持必改/禁改路径和隐藏验收器；回归命令、资源预算和交付条件后续补充。
- [x] `AcceptanceContract` 在运行前固定，由 Runner 持有，隐藏 verifier 不进入 Agent History。
- [x] Runner 成为组装根；`Agent.run()` 继续只负责同步决策循环。
- [x] 允许 Chat 传入共享 History，但不让 entry 层自行绕过 Harness。
- [x] worktree/sandbox/session 是执行策略，不散落在入口分支中。

### 入口迁移

- [x] `agent run` 迁到 Runner，保持 CLI 行为兼容。
- [x] `agent chat` 每轮迁到 Runner，保持 session checkpoint 和实时显示。
- [x] GitHub Issue 入口重命名或包装为 `fix-pr`，迁到 Runner。
- [x] 自动 PR 的 push/create PR 只在确定性验收通过后执行。
- [x] API 暂不扩展新功能；评估删除、冻结或改为薄 Adapter，不在本阶段重写 UI。
- [ ] 删除各入口重复的 backend/config/registry/AgentConfig 组装代码。

### 测试

- [ ] 三个入口在同一 fake task 下产生一致的 core run trace。
- [x] Chat 仍能跨轮共享 History 和恢复 Session。
- [x] Run 的 direct/isolate 行为与结果策略兼容。
- [x] fix-pr 测试失败或无 diff 时不 push、不创建 PR。
- [x] RunResult 区分 Agent/Completion Guard 结果、独立验收状态和交付状态；模型 FINISH 的单独状态仍待后续 Trace/领域字段补充。
- [x] push 失败保留本地 artifact；PR 失败可重试且不重复 commit/push。
- [ ] 所有入口都能触发 Hook、Permission、Trace 和 cancel。

## P1-2：可追溯 Context Compaction

### 设计

- [x] 定义 `CompactionStrategy`，通过 `prepare_next_turn` 注入。
- [x] 触发依据使用上下文 token 压力，不仅是 message count。
- [ ] 区分 must-keep、summarizable、droppable、retrievable 四类内容。
- [ ] 保留用户原始验收条件、当前计划、未解决错误、最新测试、最新 repo revision 和 retained tail。
- [x] 完整工具输出保留在 EventLog，以 `event_ref` 回查。
- [ ] checkpoint 记录源 event 范围、before/after token、repo revision、summary prompt/model version、hash。
- [ ] 明确摘要失败、取消和超窗重试语义。
- [x] 不让摘要成为仓库当前状态的权威源；代码状态仍由 repo/test/diff 确定性读取。

### 测试

- [ ] 20～30 轮后的早期硬约束仍保留。
- [ ] Action/Observation 不产生孤儿消息。
- [ ] retained tail 顺序稳定。
- [ ] compaction 取消不会写半个 checkpoint。
- [ ] summary 失败时保持旧 context 或明确失败，不损坏 session。
- [ ] resume 后可以从 checkpoint 重建有效 context。
- [x] event_ref 能回查被压缩的工具结果。
- [ ] Repo revision 变化时旧摘要不会被误当成当前状态。

### 消融

- [ ] 固定模型、prompt、repo、任务、温度/随机性设置。
- [ ] 对比无压缩、窗口裁剪、可追溯压缩三组。
- [ ] 每组重复运行并保存原始 Trace。
- [ ] 报告任务成功率、Token per solved task、p50/p95 latency、工具调用数、回查次数和人工接管率。
- [ ] 不在数据不足时写百分比结论。

## P1-3：Session 加固

- [x] 为同一 Session 增加跨平台文件锁和乐观 revision 检查。
- [x] 并发保存冲突不允许 last-writer-wins 静默覆盖。
- [x] 两个独立进程同时保存同一 revision 时，确定性得到一次成功和一次冲突。
- [x] CLI 对并发冲突给出 fail-closed、部分副作用及 `/resume`/`/new` 恢复提示。
- [x] Session checkpoint 关联 repo revision、round trace 路径和 compaction entry。
- [x] 恢复 pending round 时明确提示 partial side effects，且不自动重放工具。
- [x] Session 文件应用敏感信息脱敏策略。
- [x] 增加 state schema v1 -> v2 迁移测试。
- [x] 暂不实现树形 Session；实施计划已记录未来 fork/checkpoint 设计入口。

## P1-4：固定 Harness 任务集

- [x] 建立 `evals/fixtures` 第一版，任务仓库可重置、验收器独立于 Agent 生成测试。
- [x] 第一版包含：单文件 bug、跨文件修改、失败测试定位、新功能隐藏测试、重构、虚假完成守卫。
- [ ] 至少包含：长历史硬约束、超长工具输出、Repo Map 刷新、compaction 回查。
- [ ] 至少包含：Hook 拒绝、工具超时、取消、进程恢复、并发 worktree 隔离。
- [x] 自动 PR 使用本地 bare remote 和 fake GitHub client，不依赖真实网络。
- [x] 评测汇总器读取 JSONL，并输出机器可读 JSON 和人类可读 Markdown。
- [x] 汇总指标包含：pass@1、false-finish rate、Token/cost per solved task、p50/p95 latency、human intervention。
- [x] EvalRunner 复用 `ExecutionRunner` 跑六个 fixture，在 History 外执行隐藏 verifier，并保存 task、trace、patch 与双方状态。
- [ ] 每个任务保存“原始验收条件—独立 verifier—Agent 生成测试—代码 diff—最终结果”证据表。

## P1-5：Repo Map 一致性、相关性与消融

### 一致性

- [ ] 定义缓存身份：仓库绝对路径、Git HEAD、工作区变更集合和必要文件指纹。
- [x] 成功的 `file_write`/`file_edit`/`edit` 调用后失效当前摘要，并在同一 Run 的下一轮全量刷新。
- [ ] 将文件变化提升为明确 repo-changed 信号，并由 next-turn 策略决定局部或全量刷新。
- [ ] 测量全量构建耗时和变更规模后，再决定是否实现增量解析；不预先制造复杂缓存层。
- [ ] 同一 Session 中修改、创建、删除、重命名文件后，下一轮摘要不能继续宣称旧结构是当前事实。
- [ ] Repo Map 刷新失败时记录错误并降级到目录/搜索工具，不能静默使用不可信缓存。

### Query 相关性

- [x] 保留静态重要性作为稳定骨架。
- [x] 根据 query 中的路径、符号和依赖邻居生成候选并在预算内前置。
- [ ] 让普通源码内容关键词参与相关性评分。
- [x] 默认实现保持确定性且可离线测试；第一版不引入模型重排。
- [x] 固定任务清单带目标文件/符号标注；无唯一目标的任务仍不计算 Recall/MRR。

### 消融

- [ ] 对比：无摘要、目录树、静态 Repo Map、Query-aware Repo Map。
- [ ] 报告目标文件 Recall@K、MRR、首次有效文件定位步数、首次有效编辑前 Token、端到端成功率、构建耗时和缓存命中率。
- [ ] 固定 Commit、模型、prompt、任务集、随机性设置和重复次数，保存原始 Trace。
- [ ] 删除或继续标记所有没有实验依据的“定位时间缩短 50%/Token 减少 33%/耗时减少 20%”表述。

## P1-6：自动 PR 真实案例与面试证据包

- [x] 选择一个真实或完全可复现的 Issue，冻结问题描述和独立验收条件。
- [x] 保存从 RunRequest、Agent Trace、Tool/Permission、diff、测试到 RunArtifact 的完整链路；首个案例未启用 Permission，证据中明确记为边界。
- [x] 自动 PR 只有在独立验收通过后才 commit/push/create PR。
- [x] 保存测试、push 与 PR 失败路径的状态、保留成果和重试边界；真实案例另保存 cwd、provider 空响应和提前 commit 三类失败 Trace。
- [x] 生成一张 Agent 主调用链图，区分 ReAct、Reflection、任务状态和 Completion Guard。
- [ ] 生成一张 Worktree/Docker/Permission 三层隔离图，每层标注“防止什么/不防止什么”。
- [ ] 生成 Repo Map 消融报告与 Context Compaction 消融报告，数字可从原始 Trace 重算。
- [x] 生成一个 90 秒 case study：原问题、候选方案、个人实现、失败处理、验收结果和当前边界。
- [ ] 对照 23 道面试题更新 Claim 账本；没有新证据的问题只进入复练队列，不把计划当成果。

## P2 候选：由 P0/P1 数据决定

- [ ] 多 tool call 与只读工具并行：先评估真实失败样本；设计 Action 类型、结果原序回填、batch terminate、取消和预算语义后再实现。
- [ ] 长任务 lease/heartbeat/fencing：仅当 API/后台 Worker 继续作为正式产品入口时提升优先级。
- [ ] MCP Tool Adapter：必须复用已经稳定的 schema、Permission、Hook、timeout、cancel、Trace 和结果截断。
- [ ] 多 Agent：只有单 Agent bad case 和收益实验能证明必要性时再实现。

## 2026-09-15 真实 PR 合并后优先队列

- [x] 核验 `pr-test` PR #5 已合并、Issue #4 已关闭，Merge Commit 与 `main` HEAD 均为 `f5ad77c`。
- [x] P0：`clone_repo()` 不得把 `GITHUB_TOKEN` 放进 clone URL、remote、异常文本或日志；使用标准库和 Git 临时环境认证。
- [x] P0 测试：伪造 subprocess/Git clone，断言命令参数及结果不含 Token，最终 remote 为无凭据 HTTPS URL；真实私有 clone 冒烟通过。
- [x] P1：`--no-pr` 保留 `git_add`/`git_commit`，只有自动 PR 模式由交付层独占 commit。
- [x] P1 测试：分别覆盖自动 PR 与 `--no-pr` 的 registry 工具集合。
- [x] 首轮扫描 22 个现有 JSONL：provider 空响应命中 1 个 Run，`finish` 误调用命中 3 个 Run；任务异质，不作为失败率，当前不增加恢复代码。
- [ ] 完成 Repo Map 与 Context Compaction 可重算消融，再补隔离图和 Claim 账本。
- [ ] auto-merge 保持后置；当前真实项目继续人工 review/merge。

## 暂不做

- [ ] 不在 P0/P1 引入 MCP、Skill loader、多 Agent、树形 Session、向量库、在线自修改或新 UI。
- [ ] 不把 `Agent.run()` 改成 async。
- [ ] 不复制 Pi 整套 Extension 系统。
- [ ] 不因简历关键词迁移 JSON Session 到 SQLite。
- [ ] 不使用测试数量、覆盖率替代真实任务成功率。
- [ ] 不把“并发任务认领”表述为“多 Agent 协作”。
- [ ] 不在 P0/P1 顺带实现多 tool call、并行工具、lease/heartbeat 或模型训练模块。

## 2026-09-16 B2 termination 与 Context Policy v3

- [x] Completion Guard 可恢复拒绝进入 canonical History 与结构化 Trace，并允许后续修复。
- [x] 新运行区分 `SUCCESS/INCOMPLETE/FAILED/GAVE_UP/CANCELED`，记录 termination reason；
  max steps 使用 `resource_exhausted/max_steps`，Loop Detector 不再冒充 `GAVE_UP`。
- [x] 最后三步 warning 改为无精确倒计时的 ephemeral `[RESOURCE BUDGET LOW]`。
- [x] Runner 对 `INCOMPLETE` 跳过 hidden acceptance，delivery 仍只接受 Agent success。
- [x] B2 v3 以冻结 fixture、模型、8000 context budget、12 max steps 完成 9-run，保留 raw、
  Trace、diff、tokens、latency、termination、rejection 与 v2 对照。
- [ ] 不把 `n=3`、每格 1 次且模型非确定性的结果外推为稳定收益百分比。

## 新对话启动提示词

将下面内容粘贴到新的 Codex 对话：

```text
在 E:\2806\forgeAgent\forge-agent 开始实现 Forge Agent 的 P0/P1。

先完整阅读：
1. AGENTS.md
2. Forge-Agent-P0-P1-实施计划.md
3. TODO-P0-P1.md

再检查 git status、最近提交、remote、stash 和测试环境。保留 config/default.yaml 的用户修改。

实现必须证据驱动：
- Pi 本地源码位于 D:\2806\agent-piAgent\pi，基线 Commit 为 71dca871bc80b6bc97be37f0ca3189399d651fff；该仓库 packages/ai/src/api/google-shared.ts 有用户修改，不得覆盖。
- 先核对 Pi 的 packages/agent/src/types.ts、agent-loop.ts、agent-loop.test.ts 和 packages/coding-agent/src/core/agent-session.ts。
- DeepSeek Harness、Codex、Claude Code 只能依据可定位源码、官方文档或可复现实验，不能根据产品印象推测内部实现。
- 迁移设计契约，不逐行翻译。

严格按 TODO 顺序执行。第一项只做 P0-1 prepare_next_turn 最小 diff：保持 Agent.run 同步；第一轮不调用；在完整 turn 后、下一次 _build_messages 前调用；默认 None 零回归；覆盖注入、异常、取消和调用次数测试；不同时重构 History、Repo Map、TokenBudget，也不开始 MCP、多 Agent、multi-tool call 或 compaction。

完成 P0-1 并运行定向测试后，先汇报真实结果和 diff，再继续 P0-2。
```
