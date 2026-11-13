# Forge Agent 架构审计与开源 Coding Agent 横向对标

> 持久化研究索引｜启动于 2026-11-12｜仓库：`Napabana/forge-agent` 的 `dev`。
> 初始**生产源码基准**：`3e3fc48814aa15604c8a3a8a4432563854f8b6c1`。
> 该基准后的研究文档提交不改变已盘点的功能源码；每次专题开始仍须核查当前 `dev`，若有生产 Diff，先更新证据基线。
> 本研究不取代 `docs/design/` 的正式当前架构；`docs/research/` 专门保存对比、论证、评分和决策。

## 目的与完成定义

对 Forge Agent **全架构**进行源码审计、与 Pi Agent/Codex CLI/Claude Code 等成熟实现横向对比，给出可复现的 Harness/Evaluation baseline 判断和适合实习项目的改进路线。早期研究曾把 Teacher→Student→SFT→GRPO 作为候选后续方向；当前决策是不开展这些训练工作，仅保留相关章节作为历史问题与证据边界。完成标准是：**功能覆盖可核查、每项有源码/测试/对照依据、改进有成本和优先级、面试追问可复述、结论不夸大**。不以新功能数量或复刻商业系统作为目标。

## 进度与产物

| 阶段 | 范围 | 文档 | 状态 |
|---|---|---|---|
| 00 | 全仓库功能清单、源码/测试映射、跨领域审计计划 | [00-研究计划与功能总清单.md](00-研究计划与功能总清单.md) | **已完成首轮盘点**，未评分 |
| 01 | 整体架构、入口、Runner/Orchestrator、跨模块生命周期与状态所有权 | [01-整体架构与执行链路.md](01-整体架构与执行链路.md) | **已完成源码级静态对标**；待回归 |
| 02 | Model Integration、LLM Backend、ReAct、Prompt、Tool Call、Streaming、终止 | [02-Model-Integration与Agent-Loop.md](02-Model-Integration与Agent-Loop.md) | **已完成源码级静态对标**；定向协议回归待做 |
| 03 | 全部原生/控制/MCP 工具、Validation、Hook、Permission、Cancel | [03-Tool-System与执行治理.md](03-Tool-System与执行治理.md) | **已完成源码级静态审计**；P0 修复与回归待办 |
| 04 | History、Token Budget、Compaction、Context 视图和 Session | `04-Context-Engineering.md` | 未开始 |
| 05 | Repo Map、Tree-sitter、SQLite Schema、索引更新、检索及成本 | `05-Repo-Map深度审查.md` | 未开始 |
| 06 | Planning、Recovery、Loop、Progress、Replan Gate | `06-Planning与Failure-Recovery.md` | 未开始 |
| 07 | TaskEngine、Worktree、Local/Docker、取消与资源清理 | `07-Execution-Runtime与Isolation.md` | 未开始 |
| 08 | Completion Guard、Acceptance、Tests、GitHub Delivery | [08-Testing与Acceptance.md](08-Testing与Acceptance.md)、[离线验证记录](08-离线验证记录.md) | **源码审查完成，19 个独立原型用例已运行；3 个 Forge 行为刻画用例待真实仓库验证** |
| 09 | Trace、Observability、Eval、Graders、Benchmark、统计与消融 | [09-Trace与Evaluation.md](09-Trace与Evaluation.md) | **H0/H1 关键路径初审**，H01–H10 其余专题与测试仍待完成 |
| 10 | Skills、MCP、Trajectory、Evolution、Teacher/Student 与 RL readiness | `10-Extension与RL-Readiness.md` | 未开始 |
| 11 | 全功能评分、P0/P1/P2/P3 路线、Baseline 准入、面试 Q&A | `11-综合评分与改进路线.md` | 未开始 |

功能索引位于 **[00 报告](00-研究计划与功能总清单.md)**。阶段 0 已列出 **112 个候选细分审计对象**；后续允许进一步拆分，但不允许不留痕删除审计对象。

## 使用与续接

每个新对话先读取本 README、00 全清单、[审计决策记录](decisions/审计决策记录.md)、对应前阶段报告，再读取当前 `dev` 的实际源码和测试。不能从旧聊天记忆猜实现。历史设计动机可用 GitHub 的历史 commit `fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb` 查找已退出当前 `dev` 的 `docs/changes/`（仅作为历史决策，不代表当前状态）。

每个阶段的交付必含：① Forge 当前源码/入口/状态/异常路径；② 对照项目固定版本的**源码或官方文档**；③ 功能级横向对照表；④ 设计动机、差异、问题与风险；⑤ 10 分制评分（附证据置信度）及适用性；⑥ 修改方案、收益假设、估计成本与 P0～P3；⑦ 面试问答；⑧ 来源链接；⑨ 未解决问题及下一阶段交接。**每阶段至少独立写一份 Markdown，同时更新本索引和决策记录**。

## 证据及版本约束

- 当前实现：Forge `dev` 生产源码最优先，文档仅辅助导航；源码符号需要可定位。
- 稳定外部参考：Pi Agent、Codex CLI 以公开源码为主；Claude Code 以官方公开资料为主，不虚构内部实现；专题可补 Aider、SWE-agent、OpenHands。
- 测试分层：文件存在 ≠ 测试已执行；单元测试通过 ≠ real-model benchmark；单次 E2E ≠ 总体成功率。
- Benchmark A/B：任务、模型、参数、工具集、隔离、奖励/验收、seed/repetition、步数/token/时间等必须冻结且可追溯。
- 评分不以大项目功能数作为标准，按设计 2 + 正确性 3 + 安全可靠性 2 + 证据 2 + 扩展性 1，标记高/中/低证据置信度。无证据不得打确定分数。
- 实现不在研究时修改。仅发现明确 P0 级问题可记录为阻塞项，另开有验收方案的实现任务。
- 文档更新尽可能经 `scripts/check_docs.py --validate` 或本地 markdown/link 检查；工具环境无法执行时须明示。

## 阶段 0 关键观察（暂不判质量）

1. `entry/cli.py::_build_registry` 可注册 13 个原生 Tool；普通 `run` 的 `_build_run_registry` 默认禁用两个 git mutation tool，为 11 个；控制工具和动态 MCP 另计。
2. `context/repo_index.py` 使用五张 SQLite 表且设置 WAL；其主键、索引和全文 content 保存的必要性尚待阶段 05 评价。
3. `Agent.run` 是单同步 loop；isolate 使用单独 `orchestrate_run`；独立 Acceptance 在 Agent 之后，不是模型自报 FINISH。
4. 仓库 `docs/design/` 已有七专题、双向源码索引和映射 JSON；本研究复用而不重写其内容。此前历史 `docs/changes/` 与 `AGENTS.md` 已被当前 `dev` 移出 Git，旧 SHA 可溯源。

## 阶段 01 结论摘要（2026-11-12）

完整的 Forge 源码路径、Pi/Codex 固定版本、Claude 官方出处、12 项暂评分、风险分析和面试答法见 **[01-整体架构与执行链路](01-整体架构与执行链路.md)**。

- 方向：保持共享 Runner + 单同步 Agent loop，**不建议现在全面重写为 async**。
- **R01**：isolate async Orchestrator 直接同步运行 Agent，`AgentBus` forwarder 的即时调度可能被阻塞；CLI on_event / API SSE 不必然受同样影响；定向测试后再决定是否改。
- **R02**：isolate Worktree finalize 在 Runner hidden verifier 之前，discard / no-change 策略可能导致 verifier 无工作区可用；旧设计有意如此，若未来 RL env 需要 isolate+grader+discard，优先重新定义验收与 cleanup 时序。
- **R03**：`RunResult.is_success()` 仅判断 Agent 状态；不能取代 acceptance/grader；训练与 Benchmark 必须冻结 success 定义。
- **R04–R07**：async host 兼容、入口工具和配置差异、API admission 竞态、资源清理组合仍待后续专题验证。
- 本阶段**无新代码验证或 real-model 结果**，未宣称发现已复现 P0；只完成静态源码/公开资料比对。对照版本：Forge `3e3fc488`，Pi `b7294995`，Codex `1f1f6ebb`，Claude 2026-11-12 官方资料。

## 阶段 02 结论摘要（2026-11-12）

详见 **[02-Model Integration 与 Agent Loop 完整报告](02-Model-Integration与Agent-Loop.md)**，包含 B01–B12 的源码、Pi/Codex/Claude 直接对标、12 项暂评分、测试覆盖和面试问答。

- **R02-01**：`LLMResponse.action` 为单 Action；Anthropic/Chat/Responses 遇多原生 Tool Call 只提取第一个，且请求侧未明确限制模型一次只调用一个工具。建议优先明确单工具契约并补离线多调用测试，**不立即实现并行工具**。
- **R02-02**：OpenAI-compatible 非流式带 `strict=self._strict_tool_schema`；Streaming `_stream_with_tools` 未携带 strict，存在确定的配置差异。
- **R02-03**：Streaming 只要已有 tool delta 就把 `finish_reason` 归一为 `tool_calls`；需要拒绝 `length` 截断时的工具执行（条件 P1）。
- **R02-04**：Responses `failed/incomplete` 的 output 可能仍被解析为 TOOL_CALL，需要模拟失败状态的回归来确认安全边界（条件 P1）。
- **R02-05**：Forge History 主要以文本化 Action/Observation 表示工具交互，不能与原生 Provider tool_call_id transcript 混为一谈；当前不产出 SFT 数据，该项只保留为历史协议风险。
- **保留现有强项**：Provider 错误分类、bounded retry、stream chunk 后不盲目重试、Completion Guard、Loop Detector、独立验收状态。
- **本阶段未运行 pytest/模型实验**；评分为有源码和测试文本支持的暂评分，尚不能认定已复现 P0。
- 对照版本不变：Forge `3e3fc488`、Pi `b7294995`、Codex `1f1f6ebb`、Claude 2026-11-12 官方文档。

## 阶段 03 结论摘要（2026-11-12）

完整报告：[03-Tool-System 与执行治理](03-Tool-System与执行治理.md)，覆盖 C01–C14、入口工具矩阵、Pi/Codex/Claude 对照、14 项暂评分、风险登记与离线回归矩阵。

- **P0：原生工具参数命令注入。** Git 工具和 PytestTool 把模型参数拼成命令字符串，LocalRuntime 使用 shell=True；DockerRuntime 内部也通过 bash -c 解释命令。普通 CLI run 虽移除 git_add/git_commit，仍暴露 git_diff/test；git_diff 还在 strict read_only allowlist 中。当前静态代码路径完整，但本轮没有执行 payload，动态回归待做。
- **P1：工作区读取边界不完整。** Shell 只读判断不检查文件参数/cwd，cat/env/printenv 可能读取工作区外文件或环境变量；SearchText/FindSymbol 对遍历到的子文件 symlink 未逐个 resolve+workspace check；Shell/Test/Git 的 cwd 可被模型覆盖，strict read_only 也未校验 cwd。
- **P1：Permission 语义差异。** ToolEffect 并未成为所有 native tool 的通用 permission policy；PermissionManager 对 shell、路径文件工具、MCP 单独处理，其余 native tools 默认 ALLOW。Chat/API 注册 git_add/commit 时不会仅因 MAY_MUTATE_REPOSITORY 自动触发 CONFIRM。
- **MCP 边界**：默认不信任 read-only annotations、namespace、输出上限和 manager lifecycle 值得保留；需修 destructive/read_only 冲突、超时后不确定副作用、startup global deadline、全局 tool 资源上限，并明确 MCP stdio server 在 host 运行、不受 DockerRuntime 限制。
- **保留现有强项**：FileTool 最终路径 resolve、PreHook/Permission/Tool/PostHook 顺序、Permission subsystem infrastructure 分类、cancel 后保留真实 ToolResult、MCP namespacing、Trace lifecycle。
- **准入结论**：P0 修复前，不要用默认 LocalRuntime 对不可信仓库/公开 PR 执行开放式 Agent 任务；先以 fake Runtime 验证 argv 和无 shell 二次解释，再做隔离容器回归。
- **本阶段没有修改功能代码，没有运行 Forge pytest，也没有执行注入 payload/真实模型/MCP 服务**。独立 Python 临时目录检查只验证了标准库 symlink 行为，不是 Forge 测试。
- 对照版本：Forge 当前 dev e672dfd0（生产源码基准 3e3fc488）、Pi b7294995、Codex 1f1f6ebb；Claude 使用 2026-11-12 官方权限、Hooks、Sandbox 文档。

## 下一步

**当前首要执行：阶段 08 的离线状态回放与设计验证（不调用 LLM API），同时准备阶段 09 结果分层协议。** 完整的 G01–G09 对照、单任务 Trace 归因和 S08/A08 验收矩阵见 [08-Testing与Acceptance.md](08-Testing与Acceptance.md)。原阶段 04/05 仍保留，并在停止/测试协议可验证后继续。此前的阶段 04：Context / History / Token Budget / Compaction 规划： 按 00 索引 D01–D12 核查 canonical History、消息角色与 event refs、token counter、model capability/request budget、tool pruning、semantic packet、structured compaction、prepare_next_turn 与 Session persistence；重点验证工具结果压缩是否保留足够执行证据、是否错误提升不可信工具输出，以及 Teacher→Student trajectory 是否可重建。生成 `04-Context-Engineering.md`，更新本 README 与 decisions。阶段 03 的 P0 单独拆为安全修复任务，不在本阶段研究中直接修改生产代码。

## 最终研究决策与疑点记录

- [审计决策记录](decisions/审计决策记录.md)：记录决策和未解决假设，禁止把假设说成缺陷。
- 正式设计资料仍以 [Forge 当前代码架构](../../design/Forge-Agent-当前代码架构.md) 和 [源码索引](../../design/源码索引.md) 为准。


## 研究方向调整（2026-11-13）：Forge 作为 Harness Evaluation Platform

> 当前审查参考：`dev@ca396ce6f14b88dd439bebdcb3b9634645017034`；本节仅更新研究方法和顺序，不修改任何生产代码或历史实验。原 00–11 阶段编号与 112 项审查对象继续有效。

### 背景及目标边界

用户根据 Harbor Typer 1788 的 Qwen3.5-4B v5/v6/v7 运行结果，决定 Forge Agent 主要用于 Harness 设计验证和外部 Benchmark；随后进一步明确当前不开展 Teacher/Student SFT 或 GRPO。三次运行混有 Provider TPM 错误、目标 pytest 环境污染、取消及一次干净的 max_steps 失败，不能合并计算模型成功率。

**横向设计审查不取消、不重置**。原目标中「训练数据和 RL 准备」不再作为 Forge 的主交付，改为检查 Trace/Eval/外部工具是否具备独立、可靠、可审计的接口。Forge 的研究问题从「为了训练 Student 怎样改 Agent」转为「哪些 Harness 设计在相同模型和任务下确实提高可靠性/安全性/效果，收益是否值得额外成本」。

### 已完成报告与当前源码同步

- 01–03 继续保留为历史有版本的源码级横向审查，不静默改写旧证据和旧评分。
- 阶段 03 记录的 Git/Pytest 拼接 Shell 命令风险，已在 11 月 13 日生产修复中采用 Runtime.exec_args 和路径分离；现有修复与回归证据见 `docs/changes/2026-11-13/Forge-Agent-运行期问题修复规划与实施记录.md`。是否覆盖所有工具及工作区安全边界仍须 03 增量复核 + 07 安全测试，不将历史 P0 原样宣称为当前缺陷。
- 新增 Harbor Adapter 与 Typer 1788 真实试验：`examples/harbor/`、`docs/research/Harbor-Typer-1788实验报告.md`。这两处是后续阶段 07/09 的新证据；旧 01–03 固定 SHA 并未涵盖它们。
- 阶段 09 应审查 `evals/coding_agent/runner.py` 的 Runner 异常转 TrialResult、`report.py` 的成功率分母，以及 `__main__.py` 与生产 Context/Repo Map 配置接线；这些属于当前源码可定位的评测协议问题，不应混进模型失败率。

### 新执行次序（保留原编号）

1. **09 先行**：梳理任务成功与 Agent SUCCESS、独立 Grader、Provider/Infrastructure/Cancelled 终态；先确保模型成功率分母和实验配置可复现，避免后续 A/B 结论错误。
2. **07 + 08**：对比 Pi/Codex/Claude 的执行隔离、权限、取消、独立验收；复核 Harbor Adapter 的生命周期、Python 3.11 兼容、wheelhouse 依赖、trace fail-closed 行为（问题必须验证后修复）。
3. **04 + 05**：对比 History/Compaction/Repo Map 算法与预算，对现存专项基准复核后设计同模型 A/B。
4. **06**：对比 Planning/Recovery 控制策略；当前 Benchmark V1 R2 baseline vs Full P2 各 23/24，Full P2 额外成本高，但样本很小且配置联合改变，不能直接归因，需拆分消融。
5. **10**：对比 Skills/MCP 与轨迹可审计性；Evolution 作为可选后处理，不把 SFT/GRPO 实现塞回 Forge。
6. **11**：综合设计评分、证据置信度、改进 ROI、Harness Evaluation Baseline 准入决策和面试材料。00–03 不再执行一次，仅做当前 dev 差异更新。

### 两种证据分开

**设计横向审查**：每项要说明 Forge 当前文件/符号、Pi 或 Codex 的公开代码、Claude 的公开行为、设计取舍、风险、适用条件、实现成本及置信度；不同实现不自动代表缺陷。

**Harness 实证评测**：Model/Provider、版本、任务 SHA、Tool Manifest、权限、预算、重试、上下文、seed/repeats 固定；按 independent verifier 评分，同时分开 model outcome、provider failure、infrastructure failure、cancellation；提供成功率及 steps/tokens/latency 的配对代价。

任何跨项目比较都不直接用品牌整体成功率判断 Forge 某一模块好坏；外部 Benchmark 优先验证本项目实验结论能否迁移，不替代源码设计审查。

## 2026-11-13 补充决策：Python-only 与单任务模型对比优先

本轮进一步收敛执行范围，详见 [Python-only Harbor 与 Qwen3.8-27B 试验实施规划](../../changes/2026-11-13/Forge-Harness评测与Python-only试验实施规划.md)。

- Harbor 外部 Benchmark 本轮只保留 Python 任务；Go 任务及跨语言 Test Tool 暂不实施。此前记录的 63 个 Python 任务数须以当前 manifest 和实际 task 目录重新核验。
- `default/tiangolo__typer-1788` 的 Qwen3.8-27B 单任务 Pilot R1 已执行一次：任务 commit 固定为 `959a356fec72cedcfd4ee3f84790789f8e961989`，Baseline ReAct、35 max steps，Planning/Recovery/Skills/MCP 关闭。35 steps 是用户根据 ¥5 预算明确选择的实验参数，不冒充与 v7 的严格同预算对照。
- Qwen3.8-27B 在 Step 13 正确修改生产代码，trial 内独立 Verifier 为 reward 1.0、F2P 1/1、P2P 22/22、无 regression；但第 30 次模型调用期间被 Harbor 外层 1800 秒 timeout 取消，Trace 没有 `run_terminated`。因此该结果是“功能通过、rollout 非自然终止”，不是完整 Forge SUCCESS。
- 历史 Qwen3.5-4B v7 是一次基础设施干净的 max_steps 未完成样本：40 steps、1,764,554 total tokens、Provider cost 未记录、历史 job 未执行 Harbor verifier。v5 Provider 429、v6 环境污染后取消，均不可计入模型成功率。
- Qwen3.8-27B 的 29 个完成调用为 868,723 total tokens；按 2026-11-13 SiliconFlow 价格快照估算约 ¥2.78。Provider 实际 cost 字段为空，第 30 次 started 请求是否计费未知，故不声明精确扣费。
- Pilot 使用独立 config 和与 v7 相同的 wheel，没有修改仓库默认模型配置。完整报告及脱敏 Trace/console/result/Verifier/config/command/manifest 见 [Pilot R1 归档](../harbor-typer-1788-qwen38-27b-pilot-r1/报告.md)。
- 当前不自动进入 12-task Pilot，也不开展 SFT/GRPO。若未来继续 Harness 评测，先修复并冻结 Harbor task timeout 与 adapter timeout 的双层配置；单次模型对比不构成总体成功率或 Harness 因果结论。

## 2026-11-14：Pilot R1 专项横向对标（研究中）

- [专项-Pilot-R1-停止权限与Token成本对标.md](专项-Pilot-R1-停止权限与Token成本对标.md) 已将 Typer 1788 的完整 Trace 同 Pi Agent turn-end hook、Codex 命令解析/沙箱审批、Aider 动态 Repo Map 的固定源码进行逐项对照。
- 核心事实：Step 13 改对、Step 18 focused test 通过、Harbor reward=1.0；之后继续 11 次模型调用且无 FINISH、无 completion_rejected，最终 Harbor AgentTimeoutError；测试通过后的 375,818 tokens 约占总数 43.3%。
- 待决策：不直接删 Completion Guard，不直接允许所有 Shell，不直接将 Repo Map 设为 1K。优先做结果状态真值表、Completion Evidence/测试 provenance 离线回归、凭据隔离审查、Repo Map counter/预算消融。
- 本专项是 08/03/04/05/09 的跨阶段增量审计，**不代表 112 项已完成或任何修复已执行**；暂不修改功能代码、不调用真实模型 API。

## 2026-11-14 阶段 08 收口：Completion / Testing / Acceptance / Delivery

- [阶段 08 完整报告](08-Testing与Acceptance.md) 已完成原阶段 00 的 G01–G09 全项目静态审计，梳理 Agent FINISH Guard、测试结果、内容指纹、独立 verifier、Worktree 清理、PR 交付以及失败 Trace；固定运行源码 `ca396ce6`，对照 Pi/Codex/Claude/Aider。
- 核心事实：Pilot R1 没有 FINISH 或 `completion_rejected`，不能将超时归因于 Completion Guard 误拒绝。Step 18 的 focused PASS 之前已有 Step 16 的未跟踪临时文件，不能据此自动提交；`last_test_passed` 是全局最近 test 布尔值，缺少可信测试来源和快照绑定。
- 新增可验证风险：`_apply_no_progress_guard` 对已验证不变的仓库状态重置计数，可能使多余只读检查不触发无进展提醒；`deliver_pull_request` 使用 `git add --all`，路径合同未禁止时临时文件可能进入 PR。
- 暂不实施 `pytest PASS → SUCCESS`、不取消 Completion Guard、不修改 Runner hidden verifier 跳过规则。首选候选是 **CompletionCandidate / WrapUpRequired + TestEvidence provenance + 独立 outcome**，先跑离线 S08/A08 反例；目前**只有设计/现有测试阅读，没有新执行 pytest 或付费模型**。


## 2026-11-14 阶段 08 离线原型进展

- [阶段 08 离线验证记录](08-离线验证记录.md)：通过独立纯函数原型 `evals/completion_candidate_probe.py` 定义 NotReady / WrapUpRequired / CandidateReady；`tests/test_completion_candidate_probe.py` 在 Python 3.13.5、pytest 9.0.2 环境运行 **19 passed**，不依赖真实模型与项目生产接口。
- 在 `tests/test_agent_completion_guards.py` 追加 3 个现状刻画用例，分别覆盖后续诊断测试覆盖先前 focused pass、未跟踪临时文件未被 Agent Guard 阻挡、已测试状态中的无进展判定。因当前工作环境无仓库 checkout（git clone DNS 失败），**未执行这 3 个与原有 Forge 全套测试**。
- 本轮不改变 Agent Loop、Permission、Runner、Harbor、Prompt 或 Token Budget；原型通过率不能替代真实 Forge 代码回归，更不能推断 Benchmark 成功率。后续可直接在用户本地 Python 环境执行上述记录提供的无 API 测试命令。


## 2026-11-14 阶段 09 H0/H1 初审

- [Stage 09 当前源码问题与测试方案](09-Trace与Evaluation.md)：源码确认 `evals/coding_agent/runner.py::_run_trial` 捕获异常后仍可构造 `real_model_executed=True` / `execution_status=executed` 的失败行，而 `report.py::build_report` 会将全部 real-model rows 纳入观测成功率分母；另确认 eval factory 与 CLI 的 Repo Map / Context Policy / Test Interpreter 配置不一致。
- 在 `tests/test_coding_agent_eval.py` 追加两个当前行为刻画测试（Runner 异常被计为模型失败、Runner 工厂异常中断整个 suite），**尚未在完整 Forge 仓库中执行**。Stage 09 仍是关键路径初审，不能声称全部 H01–H10 或外部 Pi/Codex Trace 对标已完成。
- 优先级保持：先冻结 scoreability/failure domain/Verifier 结果，后解决 Eval 和生产配置复用，再开展多任务 A/B。
