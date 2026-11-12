# Forge Agent 架构审计与开源 Coding Agent 横向对标

> 持久化研究索引｜启动于 2026-11-12｜仓库：`Napabana/forge-agent` 的 `dev`。
> 初始**生产源码基准**：`3e3fc48814aa15604c8a3a8a4432563854f8b6c1`。
> 该基准后的研究文档提交不改变已盘点的功能源码；每次专题开始仍须核查当前 `dev`，若有生产 Diff，先更新证据基线。
> 本研究不取代 `docs/design/` 的正式当前架构；`docs/research/` 专门保存对比、论证、评分和决策。

## 目的与完成定义

在开展 Teacher→Student→SFT→GRPO 前，对 Forge Agent **全架构**进行源码审计、与 Pi Agent/Codex CLI/Claude Code 等成熟实现横向对比，给出可复现的 baseline 准入判断和适合实习项目的改进路线。完成标准是：**功能覆盖可核查、每项有源码/测试/对照依据、改进有成本和优先级、面试追问可复述、结论不夸大**。不以新功能数量或复刻商业系统作为目标。

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
| 08 | Completion Guard、Acceptance、Tests、GitHub Delivery | `08-Testing与Acceptance.md` | 未开始 |
| 09 | Trace、Observability、Eval、Graders、Benchmark、统计与消融 | `09-Trace与Evaluation.md` | 未开始 |
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
- **R02-05**：Forge History 主要以文本化 Action/Observation 表示工具交互，不能与原生 Provider tool_call_id transcript 混为一谈；Teacher/Student SFT 输出协议需要预先冻结。
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

**阶段 04：Context / History / Token Budget / Compaction。** 按 00 索引 D01–D12 核查 canonical History、消息角色与 event refs、token counter、model capability/request budget、tool pruning、semantic packet、structured compaction、prepare_next_turn 与 Session persistence；重点验证工具结果压缩是否保留足够执行证据、是否错误提升不可信工具输出，以及 Teacher→Student trajectory 是否可重建。生成 `04-Context-Engineering.md`，更新本 README 与 decisions。阶段 03 的 P0 单独拆为安全修复任务，不在本阶段研究中直接修改生产代码。

## 最终研究决策与疑点记录

- [审计决策记录](decisions/审计决策记录.md)：记录决策和未解决假设，禁止把假设说成缺陷。
- 正式设计资料仍以 [Forge 当前代码架构](../../design/Forge-Agent-当前代码架构.md) 和 [源码索引](../../design/源码索引.md) 为准。
