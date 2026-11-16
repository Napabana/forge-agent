# 阶段 08：Completion、Testing、Independent Acceptance 与 GitHub Delivery 横向审查

> 研究日期：2026-11-14；状态：**阶段 08 源码级审查已完成；方案/离线动态回归尚未执行**。  
> 本项目 PR 工作分支：`docs/qwen38-27b-pilot-r1`；阶段开始时 HEAD `d2b66dd96223b0573ce15172f4d9aa58377b4feb`。  
> **真实运行的生产源码基线**：`ca396ce6f14b88dd439bebdcb3b9634645017034`，对应 Typer 1788 Pilot R1 的 wheel；截至阶段开始的 PR 分支与该源码基线的生产代码内容一致（差异是文档及实验附件）。  
> 范围：原审计清单 **G01–G09** 全覆盖；补充 Pilot R1 特定的停止与测试证据问题。复用 [Pilot R1 专项审查](专项-Pilot-R1-停止权限与Token成本对标.md)，不修改功能代码，不调用付费 API。  
> 外部固定版本：Pi `b729499585bd7dacf6e9bc52994c6f157e35f9c9`；Codex CLI `1f1f6ebb6c156d5ab1d4745155f340b897b24f02`；Aider `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`；Claude Code 使用 2026-11-14 可访问的官方 Hook 文档，不推断闭源内部实现。

## 0. 执行摘要：先区分三个“成功”

**结论 A：当前真实失败首先是自然停止协议缺失，不是已经发生 Completion Guard 误拒绝。**

Pilot R1：Step 13 修改 `typer/core.py`，Step 18 执行 `tests/test_core.py` 得到 27 passed，之后继续到第 30 次模型调用被 Harbor 1800s 超时取消。Trace **不存在 FINISH，也不存在 `completion_rejected`**；独立 Harbor Verifier `reward=1.0`、F2P 1/1、P2P 22/22。现有 Guard 只能在模型主动 FINISH 时被调用，无法主动提示停止或提交验证。

**结论 B：模型自报完成 ≠ Agent 合规完成 ≠ 独立 outcome。**

- 模型发出 `ActionType.FINISH` 是候选完成请求；
- `Agent.run` 检查最低合同（changes、test 是否执行、latest test pass、测试快照一致），可能返回 `RunStatus.SUCCESS`；
- `ExecutionRunner` 的隐藏 verifier/路径 contract 只在 Agent SUCCESS 时运行，其状态独立写在 `acceptance_status`；
- Harbor 独立 Verifier 是外部 grader，可能在 Agent rollout timeout 后仍对最终工作区评分；其 `resolved=1.0` 与 `AgentTimeoutError` 可同时成立。
- `GitHub Issue → PR` 交付额外要求 Agent SUCCESS **且** Acceptance passed。

**结论 C：最快的合理改动不是“一次 pytest 通过即强制 SUCCESS”。**

正向的 `completion_candidate` / `wrap_up_required`、可信测试快照和独立评分需要拆开。本次 Step 18 之前在 Step 16 已创建未跟踪的 `repro_issue.py`，此时应首先提示清理临时文件/检查变更，而不能把测试通过当成可无条件直接交付。生产代码尚未实施这些设计。

## 1. 端到端当前执行协议与 Source Map

### 1.1 行为链

```text
CLI / GitHub Issue / Harbor adapter
       │ task description + requires + verifier ownership
       ▼
ExecutionRunner.run(RunRequest)
       ├── AcceptanceContract.apply(Task)  [进入 Agent 前冻结]
       ├── direct / isolate execution
       │       ▼
       │    Agent.run  [ReAct + tools + test observations]
       │       ├── model -> tool call -> observation
       │       ├── test_attempted / last_test_passed / tested_content_fingerprint
       │       └── model FINISH? -> Completion Guard -> RunStatus
       │
       ├── (isolate may finalize/discard Worktree before this point)
       ├── _apply_independent_acceptance()
       │       └── only if Agent SUCCESS -> required/forbidden paths -> hidden verifier
       └── result RunStatus + acceptance_status + trace
                    │
                    ├── GitHub delivery: both success + acceptance passed -> commit/push/PR
                    └── Eval/Harbor: outcome grading is separate
```

### 1.2 可定位文件

| 职责 | 真实源码 |
|---|---|
| Task intent、RunStatus、Action、RunResult | [`agent/task.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/task.py#L88-L164)、[RunResult](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/task.py#L298-L330) |
| Completion Guard | [`agent/core.py::Agent.run`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/core.py#L622-L723) |
| test observation 与 no-progress | [`agent/core.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/core.py#L945-L975)、[no-progress guard](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/core.py#L1259-L1325) |
| Test Tool | [`tools/test_tool.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/tools/test_tool.py#L40-L190) |
| checkout 内容指纹 | [`context/repository_state.py::repository_content_fingerprint`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/context/repository_state.py#L161-L211) |
| Runner 独立验收 | [`agent/runner.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/runner.py#L49-L160)、[独立验收](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/runner.py#L477-L597) |
| Worktree 完成/清理 | [`agent/orchestrate.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/orchestrate.py#L119-L133)、[finalize](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/agent/orchestrate.py#L390-L448) |
| GitHub Issue/PR | [`entry/github_issue.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/entry/github_issue.py#L218-L290)、[`run_on_issue`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/entry/github_issue.py#L331-L522) |
| Harbor Adapter | [`examples/harbor/forge_harbor_agent.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/examples/harbor/forge_harbor_agent.py#L158-L205)、[`result_policy.py`](https://github.com/Napabana/forge-agent/blob/ca396ce6f14b88dd439bebdcb3b9634645017034/examples/harbor/result_policy.py) |
| 主要测试 | `tests/test_agent_completion_guards.py`、`tests/test_runner.py`、`tests/test_repository_state.py`、`tests/test_github_issue_delivery.py`、`tests/test_harbor_examples.py`、`tests/test_failure_harness.py` |

## 2. G01–G09 完整功能级审查

### G01 Completion Guard：changes、tests、freshness

**代码事实**

1. `ActionType.FINISH` 才进入 Guard；正常 Tool Result 不会触发提交判断。
2. `REPOSITORY_UNCHANGED` 使用 checkout-visible 内容指纹，避免将纯 git metadata 更改当作文件修改。
3. `REQUIRED_TEST_MISSING` 只检查是否曾执行 `cfg.test_tool_names` 中的工具。
4. `LATEST_TEST_FAILED` 使用**全局最近一次** Test Tool 成败，没有记录测试套件范围或来源。
5. `FINAL_STATE_UNVERIFIED` 对照最后一次成功测试后的整个 checkout 内容指纹；后续新增、修改、删除未忽略的文件会使指纹变化。
6. 即使 `require_tests=False`，**只要 Agent 曾调用 `test` 且其最近结果失败，仍可能被 `LATEST_TEST_FAILED` 拒绝 FINISH**。
7. 单次 `test` 返回成功不代表某个特定正式测试或隐藏验收通过：测试目标由模型传入，运行期未冻结为合同级必选命令。
8. `tools/test_tool.py::PytestTool.effect` 声明 `ToolEffect.READ_ONLY`，但运行 pytest 会加载、执行任意测试代码，仍可能修改文件或读取环境；**工具效果元数据不是安全沙箱保证**。单独的 strict `ExecutionConstraints.read_only` 白名单未包含 `test`，不应与默认 Permission 混淆。应与 Stage 03/07 的执行隔离及凭据检查联合审查。

**设计判断**

- 应保留 changes、测试新鲜度和 fatal infrastructure fail-closed 的保护。
- **P0 设计缺口**：没有正向 completion candidate/收尾提示/可配置停止决策。
- **P1 语义缺口**：最近一次 test 结果作为全局阻断条件，可能混淆正式验证与临时诊断；不等于本次已实际触发拒绝。
- **P1 潜在漏洞**：若信任“任意成功 Test Tool 即满足 require_tests”，极窄或无关的测试也可让 Guard 满足最低条件；独立 Acceptance 是质量边界。

### G02 Completion Rejection → Reflection / Recovery

- Guard 拒绝写入 `completion_rejected` 事件，并将规范化的拒绝理由回灌到 history。
- Structured Recovery 开启时可基于 `FailureCategory.COMPLETION_REJECTED` 制定后续策略；关闭时依然可继续模型 loop。
- `agent/prompt.py` 已包含最小修改、测试通过后避免冗余检查、及时 FINISH 的规则；本次模型未遵循，说明**只增加类似 prompt 文本缺少实证依据**。
- `_apply_no_progress_guard` 对“已通过测试且当前内容指纹仍一致”的状态直接重置无进展计数，避免正常验证阶段误触发 Recovery；**相应地，已验证后不断进行只读检查，也可能持续不触发无进展警告**。这是源码推导出的过度验证风险，不是本次全部后续步骤的唯一成因。

**建议**：在原来 failure-aware recovery 之外，研究独立的 `verification_progress`、`wrap_up_required`，避免把正常收尾归类成失败恢复；不建议把 Recovery 和 Completion 合并成一个巨型状态机。

### G03 Repository Fingerprint 与活动日志排除

- `repository_content_fingerprint` 通过 `git ls-files --cached --others --exclude-standard` 枚举 tracked + 非忽略的 untracked 文件；哈希路径、文件内容和部分文件元信息，不仅看 `git status` 文本。
- 活动 Trace 日志可以通过 `excluded_paths` 排除，避免“每记录一步日志就使测试作废”的误报。
- 仅 staging/commit 但不改变 checkout 内容不会自动使之前的成功测试失效；测试 `test_git_commit_after_successful_test_does_not_require_retest` 覆盖该行为。
- 所有仓库可见的未跟踪复现脚本也会影响指纹：这对保护最终提交合理，但需要明确 **task source changes** 与 **ephemeral diagnostic artifacts** 的生命周期。
- **限制**：整个 checkout 的哈希仅表示内容相同；不能证明测试 suite 覆盖必要逻辑、依赖/外部服务未变化，也没有将具体哪些文件/测试覆盖范围持久化成独立证据对象。

**建议**：保持全 checkout snapshot 的保守校验，但补 changed-path classification 与 cleanup policy，**不要直接忽略任意 `test_*.py`**（测试本身可能是任务要求的正式修改）。

### G04 Independent Acceptance / Hidden Verifier

- `AcceptanceContract` 在 `ExecutionRunner.run` 开始时应用到 Task；Verifier 函数只由 Runner 持有，不进入 Agent History。
- `_apply_independent_acceptance` 只有在 `result.is_success()` 时运行；非 SUCCESS 则 `acceptance_status="skipped"`，**因此普通 Runner 不会在 max_steps/timeout 的最终工作区尝试独立评分**。
- 当 Agent SUCCESS，按 required/forbidden path 约束、hidden verifier 次序执行；异常或返回 false 则 acceptance failed，而 Agent status 仍可以是 SUCCESS。
- **Harbor 不同**：外部 Verifier 可以在 Forge Agent 被外层 timeout 后独立检查共享容器，Pilot R1 就是 `reward=1.0` + `AgentTimeoutError`。**不能把 Runner 内置 Acceptance 的条件原样当成 Harbor 的评分协议**。

**候选**：将“Agent terminal/status”和“某个时点 snapshot 是否允许提交外部评分”拆开。让 Benchmark 可以在正常/部分非正常终态（依预先冻结的协议）独立评分，但不得把评分结果改写为 Agent 自然 SUCCESS。

### G05 required_paths/forbidden_paths

- `_normalize_contract_paths` 拒绝绝对路径、`..`、空路径；`_snapshot_paths` resolve 后确认目标仍在仓库内；保存目标文件基线哈希。
- direct runner 对 contract paths 比较运行前后哈希，隔离 runner 使用 `result.worktree.changed_files`。
- 定向路径限制是合理的变更范围防护，但默认 `AcceptanceContract.from_task` 只携带两个 bool；实际路径 contract 必须由上层显式配置。
- 当前仅做 specified paths 的增删约束，不对未列出的额外文件自动封锁；例如 Pilot 的临时 `repro_issue.py` 和 `test_tmp_fix.py` 若未在 forbidden_paths，不能依赖此规则排除。
- **风险/条件**：隔离模式先 finalize worktree 再 Runner acceptance，若 `discard` 或无修改则可能无法读取目标文件。旧设计明确记录此顺序，不视为意外新增 bug；面向外部 grader 的生命周期需单独明确。

### G06 Agent / Acceptance / Delivery 状态隔离

- `RunResult.is_success()` 仅比较 `RunStatus.SUCCESS`。
- `acceptance_status`（not_requested、skipped、passed、failed）和 `delivery_status` 独立存在，不污染 RunStatus。
- `agent/orchestrate.py` 的 TaskEngine success 更新发生在 Runner hidden acceptance 之前；对 Orchestrator 本地状态为真，对外产品输出不可只显示这个布尔值。
- GitHub 交付阶段要求 `result.is_success()` AND `acceptance_status == "passed"`；该边界应该保留。
- 当前缺少整合所有结果域的标准化说明/真值表，Evaluation Stage 09 需要统一数据记录和分母口径。

### G07 GitHub Clean Check、Issue Branch、Delivery

- `run_on_issue` 在自动 PR 模式要求独立 `--verify-command`，没有则拒绝启动；自动交付先确保仓库干净并新建 issue branch。
- `_command_verifier` 通过 `shlex.split` 和 `subprocess.run(argv, shell=False, cwd=repo)` 执行，不依赖 Agent 自报测试通过。
- `deliver_pull_request` 在 Agent + Acceptance 双条件满足后，才 commit / push / PR；对已有 commit 或已 push 做重试判断，失败会保留一定本地产物。
- **P1 风险**：`deliver_pull_request` 使用 `git add --all`。如果 Agent 最终工作区还保留临时复现或 debug 文件，默认路径 contract 未阻止时，这些文件可能被打包进 PR。这个风险由源码直接推导，本次 Harbor 运行**没有进入 GitHub Delivery**。
- 验收结果与 Git diff/changed paths 应由提交前的确定性检查绑定；不要因为「Verifier passed」就默认全部未跟踪文件都是合法产物。

### G08 Git Mutation 工具所有权

- 普通 CLI `entry/cli.py::_build_run_registry` 移除 `git_add/git_commit`，自动 PR 模式也由交付层移除相应工具；由 `deliver_pull_request` 在验收后确定性提交。
- 这避免将「模型自行提交」与「独立验收后交付」混为同一职责，值得保留。
- 各产品入口工具清单仍需通过 Stage 09 的 trial manifest 冻结；不能假设所有入口都使用相同注册表。
- Agent 仍可通过某些 Shell/Git 操作影响仓库，权限和 Git 工具 SideEffect 边界由 Stage 03/07 继续保证。

### G09 Delivery Failure、Partial Artifact 与 Trace

- `deliver_pull_request` 区分 `blocked_agent`、`blocked_acceptance`、`commit_failed`、`push_failed`、`pr_failed`、`delivered`，部分失败保留本地已提交或已推送成果。
- `_record_delivery_trace` 记录后续交付结果；`tests/test_github_issue_delivery.py` 有 no-diff、acceptance fail、push failure、PR retry 和 trace 相关断言。
- 不能把“最终创建 PR”与“Agent 修复完成”用单一标志表示；即使修复/验收通过，远端 push/PR API 仍可能失败。
- 尚缺大规模真实网络故障/超时注入的动态证据；现有测试阅读不等于本轮已运行 pytest。

## 3. 核心状态真值表

以下是**状态域语义**，不是“某行触发自动 SUCCESS”的实现建议：

| Agent status | Runner independent acceptance | Harbor verifier | 正确报告 |
|---|---|---|---|
| SUCCESS | passed | pass | 自然完成，内部验收/外部评分通过（若都已执行） |
| SUCCESS | failed | fail/unknown | Agent 已正常结束，但独立验收失败 |
| SUCCESS | not_requested | pass/unknown | Agent Guard 通过，不代表独立验收执行 |
| INCOMPLETE（max_steps） | skipped | pass | Agent 未自然完成，但任务最终代码可能已解决 |
| INCOMPLETE（max_steps） | skipped | fail | Agent 未自然完成且外部任务未解决 |
| 无自然终态，external timeout | 内置 Runner 无可用结果或未执行 | pass | Pilot R1：**功能解决 + rollout 超时**，不得记作自然 SUCCESS |
| Provider/infra fatal、主动取消 | skipped/未执行 | missing | 需要独立故障分类；不能自动算成模型修复失败或成功 |
| SUCCESS | passed | missing | Runner 验收通过，**不能声称 Harbor 通过** |

**分母不能事后按结果选择**。若 Benchmark 以固定 wall-clock 预算内的最终 snapshot 为评分单位，超时后的有效 grading 是否计分必须先写入 benchmark contract；若评测“Agent 正常 FINISH 率”，Pilot R1 就不是自然成功。两个 KPI 同时保留。

## 4. 开源 Coding Agent 的真实机制横向对照

### 4.1 Pi Agent：宿主层的 turn completion hook

固定代码：[Pi `packages/agent/src/agent-loop.ts` 254–310](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L254-L310)：

- Stream assistant response，若有 tool calls，先执行并记录 tool results。
- 构造 `lastCompletedTurn`，调用可选 `config.finishTurn`。
- Hook 返回 `{action:"end"}` 时由宿主结束 loop；也可以选择继续。
- **不等价于内置代码修复 verifier，也不保证 Pi 默认进行自动任务完工推断**。

对 Forge 的可借鉴点是“宿主可观察每一轮 action/test result 并参与是否继续”，不是直接复制 Pi 的全部消息模型或并行工具能力。

### 4.2 Codex CLI：自然停机 + Stop Hook

固定代码：[Codex `codex-rs/core/src/session/turn.rs` 642–860](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/session/turn.rs#L642-L860)。

- `SamplingRequestResult.needs_follow_up` 控制是否还需继续模型请求/工具执行。
- 当没有模型 follow-up 和待处理输入时，才执行 `run_turn_stop_hooks`，必要时附带反馈继续。
- 这是一种**在模型自然准备停止时的宿主后验检查**，不能据此宣称 Codex 已实现“测试通过后主动结束”。

对 Forge 的参考是区分“模型请求停止的审核”与“工具完成后主动产生提交候选”，二者不是同一个 hook。

### 4.3 Claude Code：官方 Stop Hook 防误停

官方 [Hooks reference](https://code.claude.com/docs/en/hooks) 明确 `Stop` 可以阻止 Claude 结束，提供继续理由；是**已准备停止时**的扩展点。它也有 PostToolUse、PreToolUse 等不同生命周期点，但官方文档不能证明闭源内部存在特定的“按测试成功自动结束”算法。

对 Forge 的参考：把 Completion Guard 的拒绝/继续语义保留为可扩展事件，不将其夸大为客观任务验证。

### 4.4 Aider：测试反馈是有配置的验证，不等于自动隐式任务成功

官方 [Linting and testing](https://aider.chat/docs/usage/lint-test.html) 和 [Options](https://aider.chat/docs/config/options)：

- 对编辑文件默认 auto-lint，test suite 可通过 `/test` 或 `--test-cmd`、`--auto-test` 启用；
- 自动测试**默认关闭**，可选在修改后执行；
- 测试失败返回非零 exit 时 Aider 可以继续修改。
- Aider 不是 Harbor outcome grader，本对照不能推断其测试通过意味着 hidden benchmark resolved。

对 Forge 的参考：把“运行哪组测试、何时执行”的策略明确为配置和宿主职责，而不是仅基于任意模型选择的一次 Tool Result。

### 4.5 矩阵

| 能力 | Forge | Pi | Codex | Claude Code | Aider |
|---|---|---|---|---|---|
| 模型停机 | FINISH action+Guard | 响应/turn loop+可选 finishTurn | model needs_follow_up + Stop hooks | 官方 Stop Hook | 交互式编辑/测试循环 |
| 工具结束后宿主决策 | 目前没有通用 completion-ready | 有可选 finishTurn | Stop Hook 发生在自然准备停机阶段 | PostToolUse 可扩展；Stop 是准备停机时 | 编辑后可配置 lint/test |
| Test outcome | 最近一次 Test Tool 成败 | core 不内置通用 pytest grader | 不可从上述 turn 代码推断隐式 grader | 官方 hooks 可接测试检查，非默认隐式成功证明 | 命令配置+exit code |
| 独立隐藏验收 | Runner callback；Harbor 独立 verifier | 通用 core 未提供同等默认流程 | 未在本阶段证明同等默认流程 | 官方文档不暴露内部完整 grader | 非等价 |
| 可照搬性 | — | 借鉴 hook 层 | 借鉴停机检查分层 | 借鉴 Stop Hook 拒绝语义 | 借鉴可配置测试策略 |

外部项目行为的“没有在本阶段证明”不代表对方不支持。

## 5. 为什么 Step 18 后继续浪费：因果候选与反例

| 候选因果路径 | 已有证据 | 还需要的验证 |
|---|---|---|
| 模型本身没有主动 FINISH | Trace 无 FINISH | 不同 Prompt/模型随机性的真实对照；不能仅据单次归因 |
| 无正向 completion-ready 流程 | `Agent.run` 只在 FINISH 分支校验 | Scripted tool/test 序列可否在正确时刻给出收尾建议 |
| 自建临时测试成为最新失败测试 | Step 22/27 失败；`last_test_passed` 被覆盖 | 在同样状态模拟 FINISH，验证 `LATEST_TEST_FAILED`；本次真实轨迹未触发 FINISH |
| 临时文件使整仓快照不匹配 | Step 16 `repro_issue.py`，Step 20 `test_tmp_fix.py`；content fingerprint 含 untracked | 离线对相同文件修改序列计算快照与清理行为 |
| 测试过后继续检查不受 no-progress 警告约束 | `_apply_no_progress_guard` 对仍匹配已测试快照的状态重置计数 | 同一快照只读检查 N 次的 scripted replay |
| 工具/权限拒绝增加后续轮次 | Trace 6 次 confirm 无审批即拒绝 | Stage 03/07 先验证凭据隔离，再测不同 policy |

**重要反例**：Step 18 时确有正式 focused test PASS，但尚有 `repro_issue.py` 未跟踪。一个合理 Stop Policy 此时应该给出“请清理临时诊断文件并检查最终 diff”的 `wrap_up_required`，**不能直接以 PASS 作为交付成功判定**。

## 6. 设计候选：先选最小策略，再决定是否需要新状态

### 方案 A：只读 CompletionCandidate + bounded wrap-up（**首选研究方向**）

新增一个**不改变 RunStatus**的只读 CompletionEvidence evaluator（独立模块或小纯函数），在工具结果后记录：

```text
EvidenceSnapshot(
  repo_content_state,
  changed_paths / untracked_paths,
  required_check_results + check provenance,
  test_snapshot_state,
  unresolved_required_work,
  fatal_infrastructure_issue,
)
        -> NotReady | WrapUpRequired(reason) | CandidateReady(evidence)
```

- `NotReady`：继续正常 Agent loop。
- `WrapUpRequired`：已经有可信修复/验证证据，但要清理临时产物、修正已知失败或重新验证；**不宣布完成**。
- `CandidateReady`：满足最低可提交条件，给模型一次稳定、非重复的收尾提示，或由明确的宿主策略进入后续独立 verifier；**不能直接等于 SUCCESS**。
- 限定 completion candidate 的提示次数和额外步骤，避免守卫自身形成新循环；重复提示应在 Trace 去重。
- 对外部任务 verifier，隐藏测试信息永远不作为 Agent 选择工具的输入。
- 所有分类以 entrypoint trusted config + 真实工具证据计算，不能让模型给自己临时 pytest 打个 `required=true` 标签就获得通过资格。

**权衡**：实现相对小；需更严谨界定“可信验证覆盖”和“哪些临时文件必须清理”。目标是降低过度执行，不是无条件缩短全部任务。

### 方案 B：`SUBMITTED_FOR_VERIFICATION` 的显式提交终态（**暂缓**）

模型不 FINISH 时，宿主可在足够证据下明确提交 snapshot 给外部 grader。代价是必须升级 `RunStatus`、Runner Acceptance、TaskEngine、Delivery、Harbor adapter、Trace、报告分母与 UI。隐式自动提交若没有冻结协议可能虚报成功。只在方案 A 无法满足 unattended 评测时考虑。

### 方案 C：仅修改 Prompt / 降 max_steps（**不建议作为主方案**）

- `agent/prompt.py` 已明确“验证通过后避免冗余检查并 finish”，本次仍未停止。
- 降 max_steps 可以限制费用，但仍可能把可修复任务截断；原本 35 step 的任务实际上被 1800s 外层 timeout 中断，max_steps 没有成为有效保护。
- 可以作为 budget safety net，不能当作 Completion 协议修复。

### 与 TestEvidence 的最小组合

需要把当前 `last_test_passed` 的“最后一个布尔值”升级为候选的测试事件集合，最低字段：

- `tool_name`、`test_command/args`、`path/suite`、`status`、`error_type`、`timestamp/step`；
- `repo_content_fingerprint`，发生测试时所对应的 checkout；
- `origin`：trusted task contract / repository existing regression / ad-hoc diagnostic；
- `requirement_id`（仅能由可信任务合同定义）；
- `selection_reason`（模型选择什么测试，避免无关绿灯）；
- 不可伪造的 `verifier_source`：Agent-visible test 与 hidden acceptance 分开。

**不是**“ad-hoc 测试失败一律忽略”。如果 ad-hoc 失败揭示真实生产代码缺陷，应进入未解决问题/风险记录；若它是错误预期、只影响临时文件，应该有独立证据解释后清理，不能把它无条件当正式回归的否决。

## 7. 不调用 API 的动态反证测试矩阵（下一实施任务）

本阶段只提出测试，不声称已执行。

### Completion / Stop（新）

| ID | Scripted sequence | 期望 |
|---|---|---|
| S08-01 | 无改动→`test` PASS→模型给出 FINISH | `REPOSITORY_UNCHANGED`；不自动 SUCCESS |
| S08-02 | 修改→required targeted test PASS→工具状态未变→只读检查重复 N 次 | 只产生一次 completion candidate/收尾信号；不能无限延长 |
| S08-03 | 修改→targeted PASS→修改正式源文件→FINISH | 旧证据失效，必须重新验证 |
| S08-04 | 修改→PASS→新增 untracked repro 文件 | `WrapUpRequired`；不得直接交付 |
| S08-05 | 修改→PASS→创建错误 ad-hoc test→FAIL→清理文件 | 保留失败诊断证据、按来源决定是否需要复核，不无条件忽略也不无限追逐 |
| S08-06 | 自建无关 test PASS，required contract test 未运行 | 不允许用无关测试满足 required-check |
| S08-07 | required test FAILED、自建 test PASS | 不能以最新 success 覆盖必需失败 |
| S08-08 | test TIMEOUT / env INFRA error | 不当普通断言失败，也不得直接判任务通过 |
| S08-09 | 测试通过→git add/commit（文件字节不变） | 不强制 retest |
| S08-10 | task explicitly no tests / read-only | 不错误要求修改；但工具自选失败测试不得导致无限重试 |
| S08-11 | 模型主动 GIVE_UP、Provider Error、外部取消 | 维持正确终态；不得自动提交 SUCCESS |
| S08-12 | completion candidate 之后 hidden verifier FAIL | 应保留失败独立验收，不因 candidate 改写 Agent SUCCESS |
| S08-13 | 连续触发 candidate / wrap-up 但从未修复阻断项 | 有限提示后按专门的 terminal reason 停止，不循环烧 API |

建议测试命名与位置：`tests/test_completion_evidence.py`（新增）、`tests/test_agent_completion_guards.py`（旧守卫回归）、`tests/test_runner.py`（双状态）、`tests/test_failure_harness.py`（非正常分支）、`tests/test_repository_state.py`（快照）。

### Acceptance / Delivery（新）

| ID | 注入状态 | 期望 |
|---|---|---|
| A08-01 | FINISH SUCCESS + hidden fail | RunStatus 仍 SUCCESS；acceptance failed；Delivery blocked |
| A08-02 | INCOMPLETE + hidden verifier 注册 | 默认 Runner 不执行 hidden verifier；Benchmark 如另需评分，走明确的 eval-owned grading |
| A08-03 | Harbor timeout + verifier pass | 记录 functional pass + rollout timeout，不统一涂改为 SUCCESS |
| A08-04 | verifier missing/crash | 缺证 fail-closed，报告实际 exception |
| A08-05 | isolate DISCARD + independent verifier | 先显式定义验收-before-finalize 或不支持组合；不静默 pass |
| A08-06 | Agent 正确 patch + 临时 untracked files + Delivery | 默认不得静默 `git add --all` 进 PR；需 pre-delivery contract/cleanup |
| A08-07 | required/forbidden path 含绝对/../symlink | 拒绝越界 |
| A08-08 | 用户预先已有 tracked/untracked 改动 | 不算 Agent 本次变更；自动 PR 应保持 clean baseline 要求 |
| A08-09 | network push failed after commit | 保留本地 commit，delivery_status=push_failed；不把其当模型失败 |

## 8. 现有测试覆盖与证据置信度

**已读测试文件，尚未执行**：

- `tests/test_agent_completion_guards.py` 已有 failed test rejection、successful test FINISH、no change、test freshness、fatal infra、retest recover、git commit 不重测、max_steps 警告等；
- `tests/test_runner.py` 已覆盖 hidden verifier 不入 history、Runner SUCCESS 但 acceptance failed、INCOMPLETE 跳过 hidden verifier；
- `tests/test_repository_state.py` 已覆盖活动 Trace 排除、真实仓库内容变化；
- `tests/test_github_issue_delivery.py` 已覆盖 no-diff、acceptance failed、push failed、PR retry 和 delivery trace；
- `tests/test_harbor_examples.py` 已覆盖 `INCOMPLETE/GAVE_UP` nonzero 退出仍可交外部评分，以及缺少可信 terminal status 的部分路径；
- `tests/test_failure_harness.py` 已覆盖 provider、permission、cancel、infrastructure 和一些 Guard/Acceptance 非正常路径。

**本阶段没有找到明确覆盖**：

- Step 18 后可靠停止的正向 `completion_ready`，或已验证状态持续只读检查 N 次的终止上限；
- 不同测试的 provenance、required/ad-hoc 分类及 snapshot binding；
- 自建临时文件存在时的自动 cleanup / pre-delivery 排除策略；
- 同一结果中同时记录 Harbor reward=1 + Agent timeout 的报告真值表；
- GitHub Delivery 在临时 untracked 文件存在时应避免 `git add --all` 的白名单约束。

**验证限制**：静态代码路径可定位且真实 Pilot 轨迹可信，但本阶段没有运行 `pytest`、没有对 Forge 生产实现做新的动态故障注入，也没有做其他模型/benchmark A/B。测试文件存在与这次测试已运行不可混用。

## 9. G01–G09 暂评分（按既定模型）

评分按阶段 00 固定的 **设计 2 + 正确性 3 + 安全可靠性 2 + 证据 2 + 扩展性 1 = 10**。以下只是**静态审计暂评分，不是完整动态回归后的最终分数**；每项同时记录证据不足带来的不确定性。

| ID | 暂评分 | 置信度 | 说明 |
|---|---:|---|---|
| G01 Guard | 6.5/10 | 中高 | 拒绝式完成校验、测试快照逻辑扎实；缺正向收尾与测试来源 |
| G02 Recovery | 6.5/10 | 中 | canonical rejection 有测试；已有 test-pass 状态可规避 no-progress 警示 |
| G03 Fingerprint | 8/10 | 中高 | 字节指纹、日志排除、git metadata 区分较好；尚缺变更来源分类 |
| G04 Acceptance | 7/10 | 中高 | hidden verifier 与 Agent 独立；非 SUCCESS 跳过 verifier，外部 eval 需另定义 |
| G05 Paths | 7.5/10 | 中 | 路径规范化/基线比较完善；额外临时文件与 isolate finalize 仍有边界 |
| G06 Status | 7.5/10 | 中高 | 分层状态清楚；跨入口报告/分母需统一 |
| G07 Delivery | 6.5/10 | 中 | clean baseline、verify required、retry；`git add --all` 的临时文件风险 |
| G08 Git ownership | 7/10 | 中 | 交付层持有 commit；入口/工具差异尚需冻结 |
| G09 Delivery tracing | 7/10 | 中 | 失败分型与 Trace 已存在；真实网络异常矩阵尚不足 |

打分不应替代 P0/P1 风险排序；例如 G03 得分较高，并不意味着其性能或测试覆盖已全面验证。

## 10. 实施优先级、依赖和成本估计

| 顺序 | 工作 | 类型 | 核心交付/验收 | 粗略工作量 |
|---|---|---|---|---|
| 0 | 冻结 outcome/terminal/acceptance/delivery 四层状态与 Benchmark 分母 | 评测设计 + 离线真值表 | Pilot R1 能同时表达 reward=1 与 timeout | S：0.5–1 天 |
| 1 | G01/G02/G03：TestEvidence provenance + CompletionCandidate 原型 | 先独立纯函数/离线 scripted | S08-01..13；不触发错误 SUCCESS | M：2–4 天 |
| 2 | G04/G05：Agent FINISH、Snapshot、Hidden Verifier 与 Worktree lifecycle 关系 | 契约设计 + 离线测试 | A08-01..05；Verifier 不泄露到 Agent | M：1–3 天 |
| 3 | G07/G08：提交前文件白名单/临时产物分类 | 安全与交付协议 | A08-06..09；无关临时文件不能自动入 PR | S–M：1–2 天 |
| 4 | Stage 03/07 权限+secret 隔离确认 | 前置安全工作 | 凭据不能由模型可执行子进程读取；不因此放开所有 Shell | S–M，依运行时 |
| 5 | Stage 04/05 Repo Map/成本离线消融 | 独立课题 | 同仓库同 query，比较 token、相关文件覆盖、CPU；不主张模型成功率 | S–M |
| 6 | **最后**小规模付费 A/B | 成本敏感 | 同模型、同任务、同 budget，比较成功/误停/steps/tokens/latency | 视 API 预算 |

**设计决策建议**：当前先走 **方案 A** 的纯函数/Scripted Backend 原型和反证测试；在这些测试清晰以前**不修改 `Agent.run` 的正式终止语义**。不将独立 grader 暴露给 Agent，不合并 Runner 与 Harbor，不引入 SFT/GRPO。

## 11. 面试问题与应答边界

**Q1：为什么测试通过了 Agent 还没有结束？**  
A：Forge 的测试结果更新 `last_test_passed`，但仅当模型主动 FINISH 才执行 Completion Guard。Typer Pilot Step 18 focused test 通过，但模型继续产生 Tool Call，无 FINISH。拟在 post-tool 阶段评估只读 completion candidate/收尾条件，且不把 candidate 当任务成功。

**Q2：为什么不是 pytest 通过就自动停止？**  
A：Agent 可以选择无关测试，测试不代表任务正确；本例 Step 18 还有未清理的临时脚本。需要测试 provenance、与代码快照一致性、变更范围/收尾条件，并由独立 verifier 最终评估功能结果。

**Q3：最新 test FAILED 就不能完成，这不是太严格？**  
A：最近一次失败可能来自自建调试断言，与 trusted required check 不同；但调试失败也可能是真实 bug。要保留失败信息并检查来源/快照/覆盖，不可一概忽略，也不能让单个布尔值覆盖所有测试证据。

**Q4：Agent SUCCESS 与 Benchmark success 有什么区别？**  
A：前者是 Forge 内部完成性规则通过；后者是外部 grader 的任务结果。真实 Pilot 的功能 reward=1 但 rollout 因外层 timeout 未自然终止，必须并列报告两层指标，且分母定义在实验前冻结。

**Q5：参考了哪些成熟项目？**  
A：Pi 的宿主 `finishTurn`、Codex 的模型停机+Stop hook、Claude Code 官方 Stop Hook，以及 Aider 的可选自动测试机制。它们是不同职责的公开机制，**没有哪一个直接证明“pytest 通过即结束”合理**。

## 12. 下一阶段交接

**本阶段完成**：G01–G09 源码/测试静态审查；Pilot R1 严格事实与失败分层；Pi/Codex/Claude/Aider 对照；评分/风险登记；精确到测试用例的离线验收矩阵。

**本阶段未完成**：离线回归、动态安全验证、CompletionCandidate 实装、付费 A/B。不可把静态分析或设计建议说成已经修复。

**下一步推荐**：

1. 只针对 G01–G04 形成 **S08/A08 离线状态回放与设计验证**，不先改 production loop；先验证可解释的 Candidate/WrapUpRequired、测试 evidence 来源。
2. 从 Stage 03/07 的凭据隔离继续处理无人值守 permission profile；该项是放宽 Shell 前的阻塞项。
3. Stage 09 固定独立 grader/outcome/分母协议，Stage 04/05 按计划做 Repo Map 成本分析；等机制可测后才开新的真实 API run。

**来源**：本报告所有 Forge 结论来自固定 SHA 的源码/现有测试与 Pilot R1 Trace；Pi/Codex 来自上述公开固定源码；Claude/Aider 来自官方公开文档。代码未公开的行为不作推断。

## 13. 2026-11-14 离线研究原型增量验证（本报告静态审查之后）

参见 [08-离线验证记录.md](08-离线验证记录.md)。本报告第 8/12 节的“本阶段没有运行 pytest”描述的是**此前静态审查时点**；其后已完成以下有限验证：

- 新增 `evals/completion_candidate_probe.py`：只读 `EvidenceSnapshot` / `CheckEvidence` → `NOT_READY`、`WRAP_UP_REQUIRED`、`CANDIDATE_READY`。不接入 Forge 正式 `Agent.run`，不产生 `RunStatus.SUCCESS`。
- 新增 `tests/test_completion_candidate_probe.py`：19 条纯离线原型用例已在 Python 3.13.5 + pytest 9.0.2 本地执行，结果 `19 passed in 0.06s`；其中两例基于 Pilot R1 Step 18/22 的事实构造状态映射，不是完整 Trace 自动重放，也不评估 hidden test 成功率。
- `tests/test_agent_completion_guards.py` 新增 3 个当前 Forge 行为刻画用例，但**尚未在完整 Forge 仓库中运行**：当前容器无法 DNS 解析 GitHub，无法 clone 本仓库；只完成代码片段语法检查与源码接口静态核查。
- 原先设计的 S08/A08 大部分端到端/集成用例尚未实施；特别是 Agent Loop、Runner、Harbor、Delivery 的实际行为没有因这个原型而改变。不能据 19/19 推断 Forge 已解决停机问题。

**下一个必要事实门槛**：在具备完整 Forge checkout 的本地运行 `python -m pytest -q tests/test_completion_candidate_probe.py tests/test_agent_completion_guards.py tests/test_runner.py tests/test_repository_state.py tests/test_harbor_examples.py`（MockBackend/离线测试，不需调用 SiliconFlow）。若 3 个行为刻画用例通过，再讨论将 CompletionCandidate 转成低干扰的 post-tool 收尾信号；此前不自动增加新状态到生产 `RunStatus`。
