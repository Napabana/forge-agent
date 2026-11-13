# 专项横向审查：Pilot R1 的停止、权限、测试证据与 Token 成本

> 日期：2026-11-14；状态：**研究与设计候选，不修改功能代码**。
> 基线：Forge PR branch \`docs/qwen38-27b-pilot-r1@55da3bf11fc23b74eb694180c4fb07f52d11f973\`。
> **真实运行源码**：历史 wheel \`ca396ce6f14b88dd439bebdcb3b9634645017034\`，SHA-256 \`237521f488d541919855f834cfa344e4081744318b8ba7c3e7aaf11d938c167c\`。已比较这两个 Git refs：差异仅文档及实验附件，不存在生产源码 Diff。
> 实验输入：\`docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/trace.sanitized.jsonl\`、\`报告.md\`、\`verifier-reward-details.json\`。任务 \`default/tiangolo__typer-1788\`。

## 1. 研究问题与证据分级

本次**不以“Agent 太严格”为既定结论**，将问题分为四类：

- **事实 F**：Trace 或特定源码可以直接证明。
- **风险 R**：源码具有潜在不理想语义，但本次没有触发对应分支。
- **候选 D**：可借鉴的外部设计，不等于 Forge 必须照搬。
- **待验证 H**：可用离线测试/消融支持或推翻的工程假设。

### 1.1 本次 Trace 的事实

| 事件 | 观测 | 证据 |
|---|---|---|
| Step 13 | \`file_edit\` 修改 \`typer/core.py\` | Trace action/observation |
| Step 18 | \`test tests/test_core.py\` 为 \`27 passed\` | Trace Step 18 |
| Step 19–29 | 继续创建自建测试、重试测试与调试，而非完成提交 | Trace actions |
| Step 12、14、17、19、23、28 | 6 次 shell 返回 \`confirm\`，无审批通道而拒绝；无实际执行 | Trace permission_decision/observation |
| Step 22、27 | 自建 \`test_tmp_fix.py\` 测试失败 | Trace observation |
| Step 24、26 | 两次不指定 focused path 的 test 达到 120s timeout | Trace observation |
| 结束 | 30 次 LLM started、29 次 finished、无 \`run_terminated\`、无 FINISH、无 \`completion_rejected\`；Harbor \`AgentTimeoutError\` | Trace / Harbor result |
| 独立验证 | Harbor Verifier \`reward=1.0\`，F2P 1/1、P2P 22/22、无 regression | \`verifier-reward-details.json\` |

本试验是 **functional resolved = true，但 natural agent termination = false**。不可把没有 FINISH 说成「Completion Guard 已拦截正确任务」；实际上 **本次没有发起 FINISH 尝试**。

### 1.2 Token / 时间成本重算

以 29 个已完成 \`llm_call_finished.provider_usage\` 为准：

| 阶段 | Input Tokens | Output Tokens | Total Tokens | 按 ¥3/M 输入、¥12/M 输出的估算 |
|---|---:|---:|---:|---:|
| Step 1–12，定位问题 | 296,946 | 5,628 | 302,574 | ¥0.958 |
| Step 13–18，修复并首次 focused test 通过 | 187,458 | 2,873 | 190,331 | ¥0.597 |
| Step 19–29，通过后继续执行 | 365,527 | 10,291 | 375,818 | ¥1.220 |
| 合计 | 849,931 | 18,792 | 868,723 | ¥2.775 |

- 总额为 **估算值，不是 Provider 实扣**；第 30 次 started 而未完成请求可能有额外计费。
- 测试通过之后产生了约 **43.3%** 的已知 Token，¥1.22 估算开销。**这不是一个自动停止后已经实测达到的成本节省**，因为停止是否安全仍待验证。
- Trace 的 \`token_breakdown.repo_map_tokens\` 累计为 **463,080（本地估算）**，每次约 15,945–16,020；\`system + repo_map + tool_schemas\` 累计估算为 553,647，约占 Provider input 的 65%，但两类 token 计数器不是同一账单口径，**不可当成精确计费归因**。
- 运行配置 \`context.repo_map_budget=12000\` 与 Trace \`repo_map_tokens≈16000\` 口径不同。先核对 \`RepoMap.build\` 的 budget 单位/算法和 TokenCounter、缓存更新后的实际 map 长度，不能直接认定超 cap。
- Step 26 一次模型调用输出 6,876，其中 reasoning 6,819，模型耗时约 344 秒；另有两次 Test Tool 120s timeout 与两次 Provider 429 恢复。故 latency 与输入 Token 都存在不同原因。

## 2. Forge 当前源码：功能职责与问题定位

### 2.1 Completion：正向「可结束」与拒绝规则不对称

**源码**：

- \`agent/core.py::Agent.run\` 的 FINISH 分支（约 622–720 行）只在模型主动给出 \`ActionType.FINISH\` 时评估：
  - \`REPOSITORY_UNCHANGED\`；
  - \`REQUIRED_TEST_MISSING\`；
  - \`LATEST_TEST_FAILED\`；
  - \`FINAL_STATE_UNVERIFIED\`。
- 同文件工具结果处理（约 948–971 行）把每一次 Test Tool 结果写入全局 \`last_test_passed\`，成功时覆盖 \`last_successful_test_content_state\`。
- \`agent/prompt.py\` 已要求「minimal diff + verification passes → finish」；本次 Step 18 之后模型没有遵循，**不能只归咎于提示词缺少结束指导**。

**判断**：

- **F：** 缺少在每次可信测试通过后生成独立 \`completion_ready\` 事件、由宿主决策是否结束的流程；仅等待模型发起 FINISH。
- **R：** 作为 \`test\` Tool 的自建临时测试失败可能覆盖已通过 focused test 的 \`last_test_passed\`；若后来尝试 FINISH，则有可能触发 \`LATEST_TEST_FAILED\`，但本次**没有观察到拒绝事件**。
- **R：** 模型创建临时文件后，checkout-visible repository state 变化可能使「测试通过时状态」不再匹配最终状态；然而这是在防止未测更改的合理安全约束，不能直接删除。
- **边界：** Step 18 的 \`tests/test_core.py\` 不等于 Harbor 隐藏 F2P/P2P；不能因为此时 27 passed 就断言任务逻辑一定解决。

### 2.2 Permission：安全方向合理，但 unattended 配置缺口真实存在

**源码**：

- \`harness/permission.py::PermissionManager._check_command\`：黑名单 DENY；能证明只读的 shell ALLOW；其余 CONFIRM。
- \`harness/executor.py::ToolExecutor.execute\`：CONFIRM 且无 callback，返回 Permission Denied。
- \`tools/runtime.py::LocalRuntime.exec\`：执行子进程时默认继承 agent 所在环境；Harbor 目前把 \`SILICONFLOW_API_KEY\` 传入 Forge 运行进程。

**判断**：

- **F：** 只读目录查询和普通调试 Shell 被 \`confirm\` 后在无人值守 Trial 中拒绝，造成无效模型轮次。
- **D：** 不能因此将所有 \`python -c\`、shell、pip、curl 自动 ALLOW；Shell/Python 用户代码可读取环境变量或 workspace 外内容。
- **R（安全）：** 即使有容器，若模型可执行的子进程继承 API Key，在任务工作区运行不可信代码仍有敏感信息暴露风险。权限放宽必须先处理 subprocess 的 env 和 runtime 隔离（尽量由宿主注入 Provider 凭据，目标代码进程不获得凭据）。

### 2.3 Context：预算以单次请求为中心，缺少 Trial 级代价治理

**源码**：

- \`agent/core.py::_render_request_parts\` 每轮把 Repo Map 放入 system prompt。
- \`agent/core.py::_build_messages\` 通过 \`TokenBudget.history_limit_for_request\` 决定可见 history；\`_trace_token_breakdown\` 按 system、repo map、tools、history 记录本地估算。
- \`context/token_budget.py::TokenBudget.default_plan\` 按可用输入预算的 15% 分配 Repo Map，并通过 \`repo_map_cap\` 限定目标预算。
- \`context/compaction.py\` 主要处理单请求 context pressure，而非累计 Token/账单。

**判断**：

- **F：** 本次每步 provider input 在 19K 到 34K 左右，29 次累积为 849,931。长期重复固定内容和执行步数共同决定费用。
- **R：** Repo Map 过量或相关性较差可能使模型决策更慢、成本更高；目前只证明成本高，尚未证明具体 Map 内容无用。
- **R：** 对预算达不到 compaction threshold 的短任务，依靠 compaction 不会自动限制累计费用。

### 2.4 Evaluation：结果域必须分离

- \`examples/harbor/forge_harbor_agent.py\` 和 Harbor Verifier 分别负责 rollout / outcome。
- \`evals/coding_agent/runner.py::_run_trial\`、\`evals/coding_agent/report.py::build_report\` 仍需审查失败归因和分母。
- **当前真实结果**同时包含 \`reward=1.0\` 和 \`AgentTimeoutError\`。后续报表至少分开存储 \`resolved_by_verifier\`、\`agent_terminal_status\`、\`verifier_executed\`、\`provider/infrastructure_failure\` 和预算是否耗尽。
- **不能统一规定「所有 timeout 都剔除成功率分母」**：本次 timeout 后同一 Trial 的 Verifier 真实评分为 1.0；最终是否纳入某项 benchmark 指标，必须按预先冻结的评分协议和时间限制定义。

## 3. 开源项目源码横向对照

### 3.1 Pi Agent：宿主可以在 Tool Turn 完成后参与结束决策

固定参考：
- \`earendil-works/pi@b729499585bd7dacf6e9bc52994c6f157e35f9c9\`
- [\`packages/agent/src/agent-loop.ts::runLoop\`](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L254-L310)。

代码直接可见：一次 assistant response / tool results 后执行 \`config.finishTurn?.(lastCompletedTurn, signal)\`；返回 \`decision?.action === "end"\` 时发出 \`agent_end\`，也可以 \`continue\`。这是一种可选宿主生命周期 hook，**不是** Pi 内置“测试通过即证明修复成功”的 verifier，也不能说 Pi 默认会智能自动停止。

**Forge 可借鉴的最小机制**：在每次 Tool Result 后独立计算「有无可信完工候选」并给出结构化事件，必要时由宿主决策向模型发出收尾提示或中断后提交给外部 Verifier；状态应与 \`RunStatus.SUCCESS\` 区分。保留原有 FINISH Guard 和独立 Acceptance。

### 3.2 Codex CLI：审批策略 + 实际沙箱能力，而不是简单字符串白名单

固定参考：
- \`openai/codex@1f1f6ebb6c156d5ab1d4745155f340b897b24f02\`
- [\`codex-rs/core/src/exec_policy.rs::create_exec_approval_requirement_for_parsed_commands\`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/exec_policy.rs#L363-L520)
- [\`codex-rs/core/src/exec_policy.rs::render_decision_for_unmatched_command_for_platform\`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/exec_policy.rs#L852-L945)
- [\`unified_exec/preparation.rs::approval_requirement\`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/handlers/unified_exec/preparation.rs#L63-L109)。

源码可见：先解析命令，再结合匹配的 ExecPolicy、approval_policy、permission_profile、sandbox_permissions 和命令危险分类生成 Allow / Prompt / Forbidden；在 restricted sandbox 且不请求提权的特定策略下，未匹配普通命令可自动 Allow 并依赖沙箱隔离。这**不等于** Codex 允许所有 Python 命令无条件执行。

官方公开用户文档同样明确 \`workspace-write + on-request\` 会在工作区内执行常规命令，跨边界需要审批：
- https://learn.chatgpt.com/docs/agent-approvals-security

**Forge 可借鉴的最小机制**：明确交互式/无人值守两种 profile；区分「危险操作」与「工作区内可由沙箱约束的操作」；执行后端必须真的执行隔离，不能只靠 prompt 或字符串匹配；Token/API Secret 由模型可控制的代码执行环境外部保存。

### 3.3 Aider：Repo Map 相关性选择、可配置规模和阶段性扩大

固定参考：
- \`Aider-AI/aider@5dc9490bb35f9729ef2c95d00a19ccd30c26339c\`
- [\`aider/repomap.py::RepoMap.get_repo_map\`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L103-L160)
- [\`aider/repomap.py::get_ranked_tags_map\`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L576-L700)
- [官方 Repo Map 说明](https://aider.chat/docs/repomap.html)。

源码显示 \`map_tokens=1024\` 为 RepoMap 对象默认值；根据已经加入 chat 的文件等信息动态扩大 map，并做 ranked selection / cache。这是一个**有条件的动态策略**，不是每次强制 1K 或「Forge 必须缩为 1K」的依据。

**Forge 可借鉴的研究问题**：固定预算 vs query-aware 按需缩小；在已经定位并读取目标文件后是否降低 Repo Map 内容；压缩规模是否损害关键目标函数召回和修复正确性。若没有任务级准确率证据，不应仅凭 Token 少改变默认值。

### 3.4 对照矩阵

| 关注点 | Forge 真实行为 | 外部可验证机制 | 当前推荐 |
|---|---|---|---|
| 停止协议 | 只有模型 FINISH 才触发完成检查 | Pi 可选宿主 \`finishTurn\` 结束决策 hook | 优先研究「completion candidate / submit for verification」，不自动宣称 SUCCESS |
| 测试证据 | 单一 \`last_test_passed\`，不区分正式与临时测试 | 外部系统对正式验证和普通工具反馈有不同职责；未发现一套可直接移植的通用分类 | 先定义 evidence provenance；不单凭任意 pytest PASS/FAIL |
| 权限 | Shell 不可证明只读则 CONFIRM；CI 无 callback 即拒绝 | Codex 根据命令、sandbox、approval policy 联合决策 | 先确保环境隔离+凭据安全，再设计 unattended profile |
| 仓库上下文 | 每轮大段 Repo Map 随 system 重发 | Aider 基于相关性排序并动态预算 | 设计多预算离线质量/成本消融，不直接替换数值 |
| 费用 | Context window budget 不能约束全任务累计费用 | 外部方案并非本轮直接证据 | 新增 Trial token/cost upper bound 为候选，先测试控制语义 |

## 4. 应先做的离线验证（不调用 LLM API）

### H-S1：完成就绪提示能否减少多余轮次且不误判 SUCCESS？

**候选方案**：单独于 \`CompletionGuard\` 设计只读的 \`CompletionEvidence\` / \`SubmissionPolicy\`，记录：
- repo patch 是否存在及当前内容摘要；
- 覆盖任务需求的**可信**测试是否在当前 patch 上通过；
- 是否有未解决的必需工作；
- 是否存在未知/新增的未跟踪文件或新变更；
- 是否最近发生致命 infra failure；
- \`completion_ready\` 仅为 **candidate**，不能直接置 \`RunStatus.SUCCESS\`。

**首选最小方案**：在合格候选后给模型一次明确的「检查未完成要求、必要清理后 FINISH；不要新增无关测试」提示；为可配置的有限收尾步骤提供上限。若模型仍继续且已经达到限额，则宿主返回单独终态 \`submitted_for_verification\` / \`incomplete\`，并允许 Harbor 在工作区仍可用时按外部协议独立评分；**不能把这种状态假扮为 FINISH SUCCESS**。字段/终态命名须与既有 RunStatus 兼容性审查后再定。

**反证用例**：
- targeted test 通过，但隐藏测试会失败；不允许提前记为 SUCCESS。
- test 通过后再次修改生产代码；必须提示重新验证。
- 新增不相关文件/临时文件未清理；不可直接自报已完成。
- 模型创建了错误的自建测试并失败；系统不得将它默默视为无关而忽略。
- 模型从未修改代码却输出 FINISH；保留 Guard 拒绝。
- Provider / infra failure 和 external cancel；不得转成模型成功。
- 真实 Trial 中 Step 18 后没有自然 FINISH；离线模拟有限收尾不能导致无限循环。

**指标**：假阳性终止数（必须 0）、平均完成后额外 calls、任务级可靠性、Verifier outcome、终态比例；先使用 scripted backend 重放状态序列。

### H-T1：区分测试来源与被测试的代码快照

**候选方案**：TestEvidence 带 test origin、指令、退出码、result time、git content state/hash、目标文件与是否是 required/focused/ad-hoc；必须防模型自己通过标签将任意失败测试洗白。

**反证用例**：先通过可信测试再修改代码；先通过正式测试再失败自建测试；正式测试失败但自建测试通过；通过测试只覆盖无关路径；测试 timeout；临时文件进入工作区。

**指标**：Guard 状态正确、false finish rejection / false acceptance、与旧逻辑在相同 Scripted traces 下的差异。

### H-P1：无人值守 Profile 在不暴露密钥的前提下减少工具拒绝

**候选方案**：执行容器内的受限诊断能力（优先专用 argv 工具或解析后的 restricted shell）+ trusted source/cwd + 禁网 + 文件系统边界 + secret 隔离；文件路径必须在最终 resolve 之后检查；所有 denied / confirm 仍在 Trace 留因。

**必须先验证**：运行中的任务代码及其子进程是否能读到 \`SILICONFLOW_API_KEY\`；若答案为是，先隔离密钥，再考虑任何更宽的执行授权。

**反证用例**：shell \`env\`、Python \`os.environ\`、读取 secrets、越界 symlink、\`cd ..\`、\`curl\`、危险组合命令、重定向、pipe、依赖安装、用户脚本写仓库外。不得因为 \`pytest\` 看似测试命令就免除安全检查。

**指标**：良性命令拒绝率、危险命令绕过率（必须 0）、policy 命中可解释性、执行权限是否与沙箱强一致。

### H-C1：Repo Map Token 分区与相关性消融

1. 用相同 TokenCounter 对 \`RepoMap.build(budget=...)\` 输出计数，复核配置 cap=12000 与 Trace ≈16000 的差异；区分构建时 budget 估算、trace counter、Provider usage。
2. 固定旧 Run 的 repo 和 query，离线生成 \`none / 2K / 4K / 8K / 12K\` map，比较 map 内容、目标函数/相关文件的覆盖、tool schema 与 system prompt 大小、CPU 生成耗时。
3. 对已定位目标函数的后续 Step，离线模拟 reduced map，计算 **token-only hypothetical saving**；不声称改变 map 后仍能保持原 29 次 actions。
4. 只有在离线符号覆盖与 Scripted 用例不退化后，才进入少量同模型、同任务、固定 budget 的真实 A/B。

### H-E1：独立 outcome / rollout / budget 分类与报告

构造全部组合：Verifier 通过 + Agent 正常 FINISH；Verifier 通过 + Agent Timeout；Verifier 失败 + Agent FINISH；Verifier 缺失；Provider 429；Harness/setup fail；主动取消。

报告同时输出 verifier resolved、rollout status、scoreable/denominator policy、test provenance；**评分协议按 benchmark 定义冻结，不能事后为了漂亮数字改变分母**。

## 5. 优先级、成本与实施边界

| 优先级 | 首个研究/测试交付 | 工作量粗估（非承诺） | 是否马上修改生产代码 |
|---|---|---|---|
| P0 | H-E1：结果状态语义真值表 + 本次 Trial 回归用例 | S：0.5–1.5 人日 | 否，先冻结协议 |
| P0 | H-S1 / H-T1：Completion Evidence 与测试 provenance 的离线行为测试 | M：2–4 人日设计与验证 | 否，候选方案验证后再选 |
| P0（安全） | H-P1：API Key 流向、容器/子进程隔离、可执行命令边界测试 | S–M：1–3 人日 | 未证明安全前不能放宽 |
| P1 | H-C1：Repo Map Counter/预算一致性与多档离线消融 | S–M：1–3 人日 | 否 |
| P1 | H-Cost：全任务 Token/费用预算定义、停机/终态契约、模拟测试 | M：2–3 人日 | 否 |
| P2 | 小规模付费 benchmark A/B（若前述离线测试通过） | API 费用与失败重试成本待核算 | 等方案明确后 |

**执行路线**：本专项作为 08（Completion/Acceptance）、03（Permission 增量）、04/05（Context/RepoMap）和 09（Eval）之间的交叉证据。既有 00–11 和 112 项审计计划**不取消、不中断，也不把本专项冒充为全部阶段已完成**。优先从 H-E1 + H-S1 的纯离线决策实验开始。

## 6. 结论边界与面试追问

**可说**：
- Typer Pilot 在 Step 13 修复、Step 18 focused test 通过、最终 Verifier reward=1.0；Agent 自然结束失败。
- 这次没触发 Completion Guard 拒绝，问题首先在缺少正向提交协议和过度验证。
- 无人值守实验发生 6 次 CONFIRM→deny，权限策略与运行方式不匹配。
- 已完成调用总 868,723 tokens，测试通过后 375,818；Repo Map 本地估算是重要成本线索。
- Pi 具有宿主 turn-end hook，Codex 有解析后 policy+sandbox 的授权机制，Aider 采用动态相关性 Repo Map；这些是设计参考，不是效果已得到本项目实证保证。

**不能说**：
- Completion Guard 在这次轨迹中「误杀了成功」。
- Step 18 的公开测试直接等价于隐藏 Verifier。
- 将 Repo Map 调成 1K 必然提升成本/成功率。
- 放宽 Shell 就一定解决效率问题，或容器自动隔离了 API Key。
- 自动停止后必定节约 ¥1.22，或 27B 稳定优于 4B。
- Harbor \`AgentTimeoutError\` 的含义等于 Verifier reward=0。

**高频面试问答**：

Q：模型完成修改，为什么还耗费大量 Token？
A：当前执行循环只有在模型主动 FINISH 时才校验成功；实验中 Step 18 已有 focused test PASS，模型仍继续自建测试与无效工具请求。每轮又携带约 16K 本地估算的 Repo Map，29 次请求累计 849,931 input tokens，因此有停止纪律和上下文复用两类成本。

Q：怎样保证 Harness 自动结束不会误判？
A：区分 completion candidate、可信测试证据与独立 outcome。候选只发收尾提示或允许进入外部验收，不能自行宣称 SUCCESS；对 false-positive finish 的离线测试必须先通过。

Q：为什么不直接把 Shell 全部允许？
A：现有无交互 approval 确实造成 6 次拒绝，但用户代码执行可能继承 Provider Key。应先隔离可执行进程的凭据，再按明确的 sandbox/profile 执行受限命令，并保留越界和危险命令拒绝。
