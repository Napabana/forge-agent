# Forge Agent P0/P1 实施计划

> 制定日期：2026-09-14  
> 目标：将 Forge 从“功能较多的 Coding Agent 原型”推进为“可扩展、可测量、可恢复、可审计的 Coding Agent Harness”。  
> 当前阶段：先完成 P0/P1，不同时引入 MCP、Skill、多 Agent、树形 Session 与在线自进化。

## 1. 总体结论

Forge 当前最值得推进的主线是：

```text
prepare_next_turn
    -> 统一 Harness 生命周期与 Trace
    -> 三个产品入口收敛到共享 Runner
    -> 可追溯上下文压缩
    -> MCP Tool Adapter
    -> 固定任务集与消融评测
```

优先级判断：

| 功能 | 优先级 | 判断 |
| --- | --- | --- |
| `prepare_next_turn` | P0 | 低成本、高杠杆；为 compaction、Repo Map 刷新和状态注入提供统一 turn 边界 |
| Trace + 评测 Harness | P0 | 没有观测就无法证明 Token、压缩、MCP 或多 Agent 的收益 |
| before/after tool hook 生产语义 | P0 | 已有结构但未在所有生产链路接通，需要先固定契约 |
| 入口收敛 | P1 | Chat、Run、自动 PR 应共享同一 Runner/Harness，而不是共享 `Agent.run()` 就算完成 |
| 上下文压缩 | P1 | 工程与简历价值都高，并可复用 `prepare_next_turn` |
| 会话分离与持久化 | P1 第一版已加固 | 已实现线性 Chat Session、原子 checkpoint、恢复、仓库隔离、并发冲突保护、脱敏和 compaction checkpoint；树形 Session 后置 |
| MCP Tool 接口 | P2 | 必须建立在权限、Hook、超时、取消、截断和审计管线之上 |
| Skill 接口 | P2/P3 | 与 MCP 分开设计；Skill 属于 prompt/workflow/resource 注入 |
| 树形 Session | P3 | 价值高但改动面大，先做好线性 checkpoint 与压缩 |
| 多 Agent | P3 | 会放大权限、上下文、合并、并发和成本问题，必须由单 Agent bad case 证明必要性 |

### 1.1 按依赖拆分的执行批次

不把整个 P0/P1 合成一个大 diff，按生命周期依赖分批实施：

| 批次 | 状态 | 一起完成的内容 | 主要文件 |
| --- | --- | --- | --- |
| A：P0-1/P0-2 收口 | 已实现并定向测试 | Hooks 生产接线、格式清理、公开类型、现有 smoke test | `agent/core.py`、`agent/orchestrate.py`、`agent/task.py`、`harness/hooks.py`、`harness/executor.py`、`harness/__init__.py`、`tools/base.py`、P0-1/P0-2 测试 |
| B：P0-3 Trace v2 | 已实现第一版并定向测试 | prepare/LLM/tool 生命周期事件、耗时、错误分类和 usage 关联 | `agent/task.py`、`agent/event_log.py`、`agent/core.py`、`harness/executor.py`、Trace 测试 |
| C：P0 集中验收 | 已完成定向验收 | 集中覆盖异常、取消、Hook fail-open/fail-closed、零配置回归；只在测试暴露真实缺口时最小修正生产代码 | P0 测试；实际另收口 `harness/executor.py`、`tools/base.py`、`agent/core.py` 的错误分类与 diagnostics |
| D：P1-1 Runner | 已实现第一版并定向测试 | 统一 Chat、Run、API、fix-pr 的请求模型与组装入口 | 新 `agent/runner.py`、`agent/orchestrate.py`、`agent/task.py`、`entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py`、Runner 测试 |
| E：P1-2 Compaction | 已实现第一版并定向测试 | 通过 `prepare_next_turn` 接入可追溯压缩，保存 checkpoint 与 session 关联 | 新 `context/compaction.py`、`context/history.py`、`agent/core.py`、`agent/task.py`、`agent/event_log.py`、`agent/session.py`、`agent/session_store.py`、Compaction/Session 测试 |
| F：P1-3 Session 加固 | 最小收口已实现并定向测试 | 并发写保护、版本冲突、CLI 恢复提示、compaction checkpoint | `agent/session.py`、`agent/session_store.py`、`entry/cli.py` 及测试 |
| G：P1-4/P1-5 | 两个最小闭环已实现并定向测试 | 六类固定任务、Query-aware Repo Map、同一 Run 写后刷新、EvalRunner 证据 JSONL | `evals/`、`context/repo_map.py`、`agent/core.py`、评测脚本和测试 |
| H：P1-6 | 待实施 | 自动 PR 真实案例、证据包和面试材料 | Runner/PR 验收代码、实验报告和证据文档 |

表格描述的是预计改动面，不等于允许一次性重写所有文件。每批仍先沿调用链确认最小
落点；例如 API 已使用 `orchestrate_run()`，迁移到 Runner 时应改成薄适配，而不是
重写其后台任务与状态存储。
| Agent 自进化 | P4 | 不做在线自修改，改为离线失败归因、回归评测、人工审批和版本化发布 |

## 2. 已核验的当前实现

### 2.1 四个入口确实共享核心，但外围 Harness 不一致

- Chat 直接调用 `Agent.run(..., history=...)`：`entry/chat.py`。
- 普通 Run 直接调用 `Agent.run()`；仅 `--isolate` 经过 `orchestrate_run()`：`entry/cli.py`。
- API 经过 `orchestrate_run()` 后进入 `Agent.run()`：`entry/api.py`、`agent/orchestrate.py`。
- GitHub Issue 入口直接调用 `Agent.run()`，然后自行 push 和创建 PR：`entry/github_issue.py`。

因此，“共用 `Agent.run()`”只说明共享了同步 ReAct 内核，并不表示共享了：

- Hooks；
- Permission；
- Trace；
- Session；
- Worktree/Sandbox；
- 取消；
- Artifact 与交付策略。

目标产品入口收敛为三个：

1. `chat`：交互式、多轮、可恢复；
2. `run`：一次性本地任务和自动化；
3. `fix-pr`：Issue/PR 自动修复与交付闭环。

API 若暂无真实使用方，可退出简历主叙事；源码是否删除不是 P0/P1 的目标。

### 2.2 当前上下文与会话能力

已经实现：

- `ConversationHistory` 窗口裁剪；
- `TokenBudget` 分区预算和 Action/Observation 单元保留；
- Repo Map 缓存与显式失效；
- 按仓库隔离的线性 Chat Session；
- 版本化 `state.json`；
- 原子替换式 checkpoint；
- `--continue`、`--resume`、`--no-session`；
- pending round 中断识别；
- History、round、step、token 统计持久化。

当前不能声称：

- 语义上下文压缩；
- 树形 Session；
- 并发安全 Session；
- 完整确定性 replay；
- 全请求精确 Token/成本账本。

### 2.3 当前 Hooks 与 Trace 能力

`harness/hooks.py` 已定义 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`；`ToolExecutor` 已接入工具前后 Hook 与 Permission。但是：

- `orchestrate_run()` 当前构造 `ToolExecutor` 时没有注入 Hooks；
- 普通 Run、Chat、GitHub Issue 的组合路径不完全一致；
- Post Hook 当前只能观察，返回值被忽略；
- JSONL 已记录 Action、Observation、Reflection、Permission、Worktree 等事件，但缺少统一 span、阶段耗时、Prompt 分区 Token、Hook 和 compaction 事件。

## 3. P0：建立稳定生命周期边界

### 3.1 `prepare_next_turn` 策略插槽

第一版目标：在不改变 `Agent.run()` 同步模型、不重构 History/Repo Map/TokenBudget 的前提下，提供下一轮 LLM 调用前的唯一扩展点。

推荐最小接口：

```python
PrepareNextTurn = Callable[[PrepareNextTurnContext], PrepareNextTurnResult | None]
```

建议上下文：

```python
@dataclass(frozen=True)
class PrepareNextTurnContext:
    task: Task
    step: int
    history: ConversationHistory
    repo_map: RepoMap
    token_budget: TokenBudget
    cancel_event: object | None
```

第一版结果只支持明确、可审计的有限操作。优先考虑返回状态标记或替换后的下一轮依赖，不暴露整个 `Agent` 实例。

需要先固定一个重要语义：参考 Pi，`prepareNextTurn` 在一个完整 turn 结束、循环确认还要继续后调用，不在第一次模型请求前调用。Forge 应沿用这一 turn 边界语义：

- step 1 不调用；
- 工具结果已写入 History；
- FINISH/GIVE_UP/取消后不调用；
- step N 的准备结果影响 step N 的 `_build_messages()`；
- callback 前后检查取消；
- callback 异常转为结构化失败并写事件，不静默吞掉；
- 默认 `None` 完全保持旧行为；
- 第一版仅同步 callback，不把 `Agent.run()` 改成 async。

与 Pi 的差异需要明确：Pi 可替换 context/model/thinking 状态，Forge 第一版只迁移“下一轮准备策略”契约，不迁移动态模型切换和完整 AgentContext。

### 3.2 固定 Tool Hook 语义

第一版执行顺序：

```text
构造 ToolUseBlock
  -> Pre Hook
  -> Permission
  -> Tool Execute
  -> Post Hook
  -> Observation Transform（P1 或以后）
  -> History / EventLog
```

第一版契约：

- Pre Hook 可以拒绝执行；
- Permission 负责 allow/confirm/deny；
- Post Hook 只观察，不改写结果；
- Hook/观察者错误是否 fail-open 或 fail-closed 必须按阶段明确，不能全部吞掉；
- 所有入口必须经由同一 ToolExecutor 配置路径。

Pi 当前允许 `afterToolCall` 对 content/details/isError/usage/terminate 做显式字段覆盖。Forge 不立即复制该能力。若 P1 需要脱敏、截断或标准化，应增加显式 `ObservationTransform`，同时区分 raw result 与 model-visible result，避免审计歧义。

### 3.3 Trace v2 最小事件模型

继续使用 JSONL，不在 P0 引入 OpenTelemetry。统一关联字段：

```text
run_id / session_id / turn_id / step_id / span_id / parent_span_id
```

P0 至少记录：

- next-turn prepare 开始、结束、失败、耗时；
- LLM 开始、结束、重试、input/output token；
- Tool 开始、结束、耗时、结果大小和错误分类；
- Hook 与 Permission 决策；
- 最终状态与取消位置。

所有 Trace 字段必须经过敏感信息治理；命令参数、环境变量、模型原始响应不能无条件原样写入。

## 4. P1：把能力真正接入产品 Harness

### 4.1 统一 Runner

目标结构：

```text
Entry Adapter
    -> RunRequest
    -> ExecutionHarness / Runner
        |- session/history
        |- prepare_next_turn
        |- hooks + permission
        |- trace
        |- cancellation
        |- worktree/sandbox
        `- Agent.run
    -> RunArtifact
```

入口只保留输入输出差异：

- Chat：Session、共享 History、终端交互；
- Run：一次性任务参数；
- fix-pr：读取 Issue、交付前验收、commit/push/PR。

尤其需要修正 GitHub Issue 入口绕开 `orchestrate_run()` 和生产 Harness 的问题。自动 PR 必须满足：测试失败不创建 PR、没有 diff 不创建 PR、push/PR 失败可恢复、交付行为可审计。

2026-09-14 最小实现状态：新增同步 `ExecutionRunner` 与 `RunRequest`，统一 direct 与
isolate 的 Agent/Executor/EventLog 组装；Run、Chat、API、GitHub Issue 已迁入该入口。
产品层仍负责 backend、工具注册表、终端显示、API 队列和 GitHub 交付。自动 PR 的独立
验收与幂等交付尚未完成，不能把“Agent 成功”表述为“PR 已可靠交付”。

### 4.2 可追溯上下文压缩

通过 `prepare_next_turn` 实现，不往 `Agent.run()` 添加 compaction 分支。

上下文分层：

- 不压缩：系统约束、用户验收条件、当前计划、未解决错误、最新测试、最新仓库状态；
- 可摘要：早期讨论、重复搜索、长工具输出、已完成中间步骤；
- 可丢弃：重复成功提示、无信息日志、被新状态覆盖的旧状态；
- 可回查：完整工具结果保留在 JSONL，通过 `event_ref` 回查。

每个 compaction checkpoint 记录：

```text
压缩前/后 token
覆盖的 event 范围
必须保留事实
仓库 revision
摘要模型与 prompt 版本
摘要哈希
retained tail 边界
```

2026-09-14 第一版采用确定性 `extractive-v1`：只在 history token 压力达到阈值后，
通过 `prepare_next_turn` 保留首条任务约束与最近完整 tail，并把旧消息压成有 hash 的
摘要。checkpoint 记录 before/after token、Repo revision、source Event IDs、方法版本、
hash 与 tail 大小，Chat Session 同步持久化。该版本没有调用摘要模型，尚未完成
20～30 轮长历史、取消/失败原子性和三组消融，因此不能声称语义压缩收益。

触发条件应基于实际上下文压力，而不只是消息条数。

### 4.3 Session P1 加固

- 增加文件锁或乐观版本号，阻止同一 Session 并发覆盖；
- checkpoint 关联 repo revision 和 compaction entry；
- 明确恢复后如何处理 partial tool side effects；
- 增加敏感信息脱敏；
- 暂不迁移 SQLite，除非并发查询或树形分支形成真实需求；
- 暂不实现树形 Session，先保证线性 checkpoint 可验证。

2026-09-15 第一版已经落地：Session schema 升级到 v2，并兼容读取 v1；同一 Session
写入同时使用跨平台文件锁和 revision 比较，旧副本保存时抛出明确冲突，不再静默覆盖；
写盘副本对 Authorization Bearer、常见 key/token/secret/password 和 `sk-` 形式秘密
脱敏，运行时内存消息不被改写。Chat checkpoint 已保存 repo revision、每轮 trace 路径
和 compaction entries。树形 Session、SQLite 迁移仍按计划后置。

## 5. Token 与成本评估

不能只记录总 Token。至少拆分：

- input/output token；
- cache read/write token（Provider 支持时）；
- system/tool schema/Repo Map/history/observation token；
- compaction token；
- 每轮上下文窗口占用比例。

核心指标：

```text
Token per solved task = 总 Token / 验收通过任务数
Cost per solved task = 总模型费用 / 验收通过任务数
Context pressure = 单轮最大输入 Token / 模型上下文上限
Compaction ratio = 1 - 压缩后 Token / 压缩前 Token
```

同时记录 p50/p95 Token、p50/p95 耗时、首次定位正确文件的步骤、首次有效修改前 Token、工具调用次数、压缩后回查次数、人工接管率和虚假完成率。

最终简历只选择两三个经过固定任务集验证的强指标，不能把测试覆盖率等同于真实任务成功率。

## 6. 固定评测任务集

第一阶段已建立 6 个小型、可重复、带独立隐藏验收的编码任务；后续扩充到 15～20 个，
补齐上下文、Harness、并发隔离和自动 PR 场景。当前 fixture/verifier smoke 只证明评测
基础设施可运行，不等于 Agent 已在这些任务上取得成功率。

### 编码任务

- 单文件边界错误；
- 跨文件接口签名修改；
- 根据失败测试定位根因；
- 新增小功能并通过独立隐藏测试；
- 行为保持型重构；
- 测试通过后再次修改，验证 Completion Guard 阻止虚假完成。

### 上下文任务

- 目标文件不在 Repo Map Top-K；
- 早期约束经过 20～30 轮后仍需保留；
- 超长工具输出后继续完成；
- 文件修改后 Repo Map 刷新；
- 压缩后通过 `event_ref` 找回早期事实。

### Harness 与可靠性任务

- Pre Hook 拒绝危险命令；
- Post Hook 异常语义；
- Permission allow/confirm/deny；
- 工具超时、输出截断、未知工具、格式错误；
- next-turn callback 前后取消；
- 进程中断后恢复 Session；
- 并发任务认领和 Worktree 隔离；
- 沙箱不可用时禁止虚假成功。

### 自动 PR 任务

- 本地 bare remote + fake Issue/PR client 完成 Issue -> branch -> 修改 -> 测试 -> commit -> push -> PR；
- 测试失败或无 diff 时不创建 PR；
- push 失败保留成果；
- PR 创建失败可重试且不重复 commit/push。

## 7. P2 之后的边界

### MCP

MCP 的简历价值不是“支持协议”，而是本地工具和远程 MCP 工具统一经过 ToolRegistry、权限、Hook、超时、取消、结果截断与审计链路。第一版用 fake MCP transport 验证 schema 冲突、超时、取消、超大结果、敏感信息和危险工具权限。

### Skill

Skill 属于指令模板、资源和任务流程，不等于 MCP。第一版只读加载和显式启用，不做自动安装、依赖解析和热更新。

### 多 Agent

只有固定任务集证明单 Agent 在可拆分多模块任务、实现/验证同源偏差或大仓库定位上存在稳定失败，才实现最小 `Planner -> Workers -> Verifier`。子任务需要独立预算、trace、worktree 和确定性合并。

### 离线策略迭代

不称“Agent 自进化”。实现形式为：失败轨迹分类 -> 生成 prompt/config 候选 -> 固定回归集对照 -> 人工审批 -> 版本化发布和回滚。

## 8. 参考实现与证据原则

证据优先级：

1. 固定 Commit 的可定位源码与测试；
2. 官方设计文档/API 文档；
3. issue、PR 和维护者讨论；
4. 二手文章只用于寻找入口，不能作为实现契约。

当前已核验 Pi 本地源码：

- 源码目录：`D:\2806\agent-piAgent\pi`；
- 固定基线：`71dca871bc80b6bc97be37f0ca3189399d651fff`；
- 用户修改：`packages/ai/src/api/google-shared.ts`，研究与实现时不得覆盖；
- `packages/agent/src/agent-loop.ts`：`prepareNextTurn` 在已完成 turn 后、下一轮开始前运行；
- `packages/agent/src/types.ts`：before/after hook 的类型化返回契约；
- `packages/agent/test/agent-loop.test.ts`：snapshot、block、terminate、after override 的测试证据；
- `packages/coding-agent/src/core/agent-session.ts`：产品层将 compaction、system prompt、tools、model 刷新组合到 next-turn hook。

DeepSeek Harness、Codex、Claude Code 在实际编码前需重新定位可审计源码或官方文档。若某产品实现不可见，只比较公开契约和行为，不推测内部代码。迁移设计契约，不逐行翻译实现。

## 9. 可形成的简历候选表述

完成 P0 并有测试证据后：

> **可扩展执行编排：** 针对上下文刷新、压缩和状态注入逻辑容易侵入 Agent 主循环的问题，设计下一轮准备策略插槽，将上下文演进收敛到 LLM 调用前的统一生命周期，并通过异常、取消和零配置回归测试保证扩展点不改变默认执行语义。

完成 Trace 与固定评测后：

> **可观测评测体系：** 围绕模型调用、工具执行、权限决策、上下文压缩和交付结果构建分层 Trace 与固定任务集，统计任务成功率、每成功任务 Token、p95 延迟、取消响应和虚假完成率，为 Repo Map、压缩及 MCP 接入提供可复现的消融依据。

真实数字只能在实验完成后补充。

## 10. 面试经历驱动的缺口复盘

来源：`E:\2806\简历\面试项目复盘\outputs\项目-forge-agent.html`。该材料包含 23 道 Forge 相关问题，覆盖 4 场面试。以下只把已出现的追问转为项目改进，不用单次回答推断整份简历掌握程度。

### 10.1 重复暴露的五个核心缺口

| 面试问题簇 | 当前已有证据 | 真正缺口 | 处理优先级 |
| --- | --- | --- | --- |
| Repo Map 内容、失效、效果与 Query 相关性 | tree-sitter 符号抽取、静态+Query-aware 确定性评分、TokenBudget、缓存失效入口、六项目标标注 | 缺修改后一致性契约、Recall/MRR 与端到端消融报告 | **P1 继续收口** |
| 长上下文保留/丢弃与历史压缩 | 窗口裁剪、Action/Observation 预算单元、线性 Session checkpoint | 消息数窗口仍可能先破坏配对；没有语义摘要、retained tail、event_ref 和恢复实验 | **P0 插槽 + P1 压缩** |
| “测试也是 Agent 写的，如何证明完成” | Completion Guard、已有测试工具、repo state/diff 检查 | 缺独立验收契约、隐藏测试、需求—测试—diff—结果证据表 | **P1 必做** |
| 工具失败、模型依赖和工具扩展 | Backend/ToolRegistry/ToolExecutor、有限模型重试、Observation 反馈 | 缺中心参数校验、统一错误分类、跨 Provider 契约测试、完整 usage/timeout 统计；多 tool call 边界需如实说明 | **P0/P1 补强** |
| Demo/toy、创新、个人贡献 | 可运行 CLI、Worktree/Docker/Permission、JSONL、自动 PR 入口 | 缺一条真实且可复现的 Issue→修改→独立验收→PR 案例，以及“参考—取舍—个人实现—验证”的证据链 | **P1 必做** |

另外三类问题主要是表达与基础知识，不应通过堆功能解决：

- ReAct、Reflection、任务状态机混用：需要一张真实调用链和状态所有权图；
- Worktree、Docker、Permission 的隔离边界：需要“防什么/不防什么”的三层隔离图和失败测试；
- Python 线程/进程、技术栈定位、求职方向：需要知识复练和能力分层，不需要为了回答而引入多进程或模型训练模块。

### 10.2 根据面试反馈调整后的 P0/P1 交付顺序

```text
P0-1 prepare_next_turn
  -> P0-2 Tool Hook + 参数校验/错误分类契约
  -> P0-3 Trace v2（先能量化）
  -> P1-1 统一 Runner + 独立 AcceptanceContract
  -> P1-2 可追溯 Compaction
  -> P1-3 Session 加固
  -> P1-4 固定 Harness 任务集
  -> P1-5 Repo Map 一致性、相关性与消融
  -> P1-6 自动 PR 真实案例与面试证据包
```

Trace 必须先于大规模消融，否则后面仍只能说“感觉 Token 少了”。独立验收必须进入 Runner，而不是继续依赖模型自己生成的测试和 FINISH 文本。

### 10.3 Repo Map 应怎样改进

面试官对 Repo Map 连续追问了“包含什么、谁维护、如何失效、有没有效果、如何结合 Query”。这说明它是当前简历最显眼、同时证据最薄的 Claim。

#### 分阶段实施计划（先修热点，再扩能力）

当前仓库的只读基线测量（102 个文件发现、77 个文件解析）显示：首次构建约 59.5 秒；跳过 `_apply_reference_scores()` 后约 3.0 秒；同一 `RepoMap` 的 warm build 约 1 ms。由此可见，当前首要问题是引用计数的“每个文件 × 每个符号 × 全文正则”热点，而不是先引入 Global Map、向量索引或新的持久化层。该测量只用于本机相对比较，不能外推为所有仓库的绝对耗时。

按以下顺序落地，后一阶段必须以前一阶段的测试和评测证据为入口：

1. **最小热点修复（当前变更）**：保留 `FileInfo`、符号所有者集合、词边界和“定义文件不统计自身引用”的现有契约；将逐符号全文正则改为每个文件一次 identifier 扫描，再按命中的名称回写 `reference_count`。补充精确边界回归测试，并以构建耗时、解析数量和 map 文本一致性做前后对比。不同时重构 History、TokenBudget、MCP 或 Agent 主循环。
2. **Query-aware 排序（P1-5 第一子阶段）**：复用现有静态 `FileInfo`，从用户 query 提取路径片段、符号名、关键词和依赖邻居，生成可解释的增量分数；无 query 时保持当前排序。先做 Recall@K、MRR、首次有效文件定位步数的固定小任务集，不先加入 embedding 或不可复现的语义黑盒。
3. **稳定骨架与本轮 Focused Map（P1-5 第二子阶段）**：把稳定的入口/核心符号与本轮候选分开渲染；候选不足时通过现有 `file_read` 等工具按需补充上下文。明确 system prompt 中骨架的固定位置，避免把每轮变化内容混入可缓存前缀。该阶段只在 Query-aware 评测证明有收益后开始。
4. **一致性与增量索引（P1-5 第三子阶段）**：先定义 Git HEAD、工作区变更集合和文件内容指纹的缓存键；命中时复用，局部修改时只重解析受影响文件及其引用邻居，无法证明一致时回退全量扫描。持久化索引应后置，先以进程内缓存验证正确性、失效和并发边界。
5. **预算、Prompt Cache 与成本评测（收口阶段）**：接通现有 `context.repo_map_budget` 配置并记录实际 map 输入规模；把“本地构建耗时”和“Provider cache hit”分成独立指标。使用固定任务集比较目录树、静态 map、Query-aware map 和 Focused Map，至少报告目标文件 Recall@K、MRR、首次有效编辑前 Token、任务成功率、构建耗时、缓存命中率及 p95；没有目标文件标注的任务只报告端到端结果。

2026-09-15 已完成第 2 阶段的最小实现：`RepoMap.build(query=...)` 在原静态重要性上
叠加路径、符号、关键词和已匹配符号使用者的确定性分数；Agent 每轮按当前任务 query
重新渲染摘要，同一仓库仍复用已扫描的 `FileInfo`。固定任务清单已标注目标文件和符号。
当前只验证了排序与缓存契约，尚未产生 Recall@K、MRR 或端到端成功率数字。

暂不实施：全局向量/图数据库、跨会话语义索引、自动摘要替换静态骨架、依赖模型自评的相关性分数。这些方案只有在上述消融显示明确瓶颈且能给出失效与回退契约后再单独立项。

P1 应形成三层设计：

1. **稳定骨架**：继续使用静态符号、import/reference、路径和大小信号；
2. **本轮相关性**：用 query 中的路径、符号、关键词和依赖邻居召回候选；只有小候选集才允许可选语义重排；
3. **一致性**：以 Git HEAD、工作区变更集合和文件内容指纹决定复用、局部刷新或全量刷新。

必须对比：

```text
无仓库摘要 vs 目录树 vs 静态 Repo Map vs Query-aware Repo Map
```

指标至少包含目标文件 Recall@K、MRR、首次有效文件定位步数、首次有效编辑前 Token、任务成功率、Repo Map 构建耗时和缓存命中率。没有标注目标文件的任务不能计算 Recall/MRR，应只报告端到端结果。

### 10.4 独立验收契约

为解决“测试也是 Agent 自己写的”这一同源偏差，Runner 应引入独立于 Agent History 的 `AcceptanceContract`。它由入口或评测夹具在运行前生成，Agent 不能在执行过程中自行降低标准。

最小契约可包含：

- 必须/禁止修改的路径；
- 仓库原有回归命令；
- 由 Harness 持有、默认不注入模型的隐藏验收测试；
- 静态检查或运行命令；
- 必须出现/禁止出现的 diff 特征；
- 基础设施失败与业务验收失败的区别；
- 超时、最大 Token、最大步骤和人工确认边界。

最终状态应区分：模型声称完成、Agent Completion Guard 通过、独立验收通过、交付成功。只有最后两层都通过才能进入自动 PR。

### 10.5 Tool/Model Harness 需要补强的边界

面试材料明确暴露：当前工具参数缺少统一严格 JSON Schema 校验；工具失败分类和模型依赖隔离还不够完整；一次多个 tool call 的处理边界也需要如实说明。

P0/P1 只补以下基础契约：

- ToolRegistry 的 schema 与运行前参数校验保持一致；
- unknown tool、invalid arguments、permission denied、timeout、tool execution、infrastructure、provider/retry-exhausted 使用稳定错误分类；
- Pre Hook 接收已经完成结构校验的参数；
- Trace 能统计每类错误和恢复结果；
- 对 Anthropic、OpenAI-compatible、Responses 使用同一组 Backend contract tests。

多 tool call、只读工具并行属于 P2：它会影响 Action 类型、事件顺序、History 回填、取消、Completion Guard 和预算，不能夹进 `prepare_next_turn` 或 Hook diff。

### 10.6 长任务与并发的处理边界

SQLite WAL、条件认领、Worktree 和协作式取消已有实现证据，但 worker 崩溃后的 lease/heartbeat 回收尚未实现。因此 P0/P1 先通过 Trace 和故障测试说明现状；只有继续保留 API/后台 Worker 作为正式入口时，才把 lease、fencing token、超时回收提升到 P2。

简历不能把“并发任务认领”说成“多 Agent 协作”。前者是任务调度和隔离，后者需要子任务拆分、通信、成果合并和独立预算，目前不存在。

## 11. 面试证据包

完成 P1 时除代码和测试外，应生成三类可复现材料：

1. **架构证据**：一张 `Entry -> Runner -> Agent.run -> Tool/Observation -> Guard -> Artifact` 主链图，以及 Worktree/Docker/Permission 三层隔离图；
2. **实验报告**：固定 Commit、模型、任务集、随机性设置、原始 Trace、失败分类和消融结果；
3. **真实案例**：一个真实或本地可复现 Issue 的验收条件、Agent Trace、代码 diff、独立测试、失败恢复和最终 PR/本地交付结果。

每个重要简历 Claim 都使用四段式证据：

```text
参考了什么 -> 为什么选择/舍弃 -> 自己实现了什么 -> 如何验证
```

### 11.1 高风险简历 Claim 边界

| Claim | 当前处理 |
| --- | --- |
| “独立开发” | 可以保留个人实现边界，但必须承认参考 Pi、Claude Code 类产品和其他开源思路，并指出具体差异 |
| “上下文工程” | 当前可说 Repo Map + TokenBudget + History Window；语义压缩完成并评测前不能写成已实现 |
| “并发任务认领” | 可说 SQLite WAL 条件更新与 Worktree 隔离；不能说多 Agent 并发协作 |
| “完整执行链路” | 可说 JSONL 审计轨迹；在 Trace v2 前不说分布式追踪或确定性 replay |
| “兼容多模型服务” | 可指向 Backend 实现；必须补 Provider 契约测试、usage 和错误映射证据 |
| “提高性能/降低 Token” | 没有 baseline、样本、重复次数和报告前一律不写百分比 |

### 11.2 面试前仍需单独复练

以下内容不等待功能全部完成也要准备：

- 60 秒项目定位：目标用户、典型输入、输出和为什么采用 CLI；
- 90 秒主调用链：ReAct、Reflection、状态机、Completion Guard 的位置；
- 90 秒失败案例：一次工具错误如何成为 Observation、如何纠正、如何终止；
- 90 秒原创性边界：参考、取舍、个人动作、测试证据；
- Python asyncio、线程、进程、子进程和服务 Worker 的适用边界；
- 技术栈按“熟练/使用过/了解”分层，求职主线保持 Agent/大模型应用工程。

## 12. 2026-09-15 F/G/H 与 P0-1 缺口复核

### 12.1 当前结论

| 范围 | 真实状态 | 仍需闭合的主要契约 |
| --- | --- | --- |
| P0-1 `prepare_next_turn` | 显式刷新与生命周期收口完成 | 共享 Chat History 去重与 Pi snapshot 对照说明仍待补充 |
| F：Session 加固 | 最小收口完成 | 自动合并不做；更广脱敏只在出现真实样本后扩展 |
| G：固定任务与 Repo Map | 一致性和 EvalRunner 最小闭环完成 | 上下文/Harness/PR fixtures、Recall/MRR 与真实消融 |
| H：自动 PR 证据包 | 独立验收、确定性交付及首个真实 PR 合并案例完成 | Token-safe clone、Repo Map/Compaction 消融、隔离图与 Claim 账本 |

当前 Query-aware 排序已覆盖路径、符号和匹配符号使用者，但普通源码内容关键词尚未
参与评分；因此 TODO 中“关键词已完成”应在对应实现批次改回部分完成。当前六个 fixture
及汇总器也不能替代真实 Agent 运行，尚无 pass@1、Recall@K、MRR 或收益百分比。

### 12.2 后续严格执行顺序

每批先完成最小代码和一个定向测试，再更新 TODO/本地日志；不重复跑全量测试。

1. **G-一致性最小闭环（已完成）**：成功的 `file_write`/`file_edit`/`edit` 调用会失效
   当前 Repo Map 摘要，下一次 `_build_messages()` 以全量刷新结果构建消息；最小契约测试
   已覆盖同一 `Agent.run()` 创建文件后下一轮可见。实际修改 `agent/core.py`、
   `tests/test_repo_map_improvements.py`；WSL 定向测试 10 passed，未实现增量索引。
2. **G-EvalRunner 最小闭环（已完成）**：新增 `EvalRunner`，按 fixture 仓库构造并
   复用 `ExecutionRunner`，在 Agent 完全返回后执行隐藏 verifier；每个任务立即写入
   task、trace、统一 diff、Agent 状态和 verifier 状态。实际修改 `evals/harness.py`、
   新增 `evals/run.py`、修改 `tests/test_evals.py`；报告器保持兼容而未修改。MockBackend
   批量覆盖现有六个 fixture，WSL 定向测试 3 passed，未调用真实付费模型。
3. **P0-1 收口（已完成）**：`PrepareNextTurnResult` 已增加显式 `refresh_repo_map`，
   callback、Trace、取消和异常处理已提取到 `_prepare_next_turn()`；`Agent.run()` 只保留
   turn 边界调用和提前返回。README 已写明第一轮不调用、Observation 已入 History、
   同步执行和终止边界。实际修改 `agent/core.py`、`README.md`、
   `tests/test_prepare_next_turn.py`；WSL 相关定向测试共 10 passed。
4. **F 最小收口（已完成）**：保持冲突 fail-closed，在 `entry/cli.py` 的实际
   `session.run_round()` 边界单独提示另一进程已写入新状态、本轮未保存和可能存在部分
   工具副作用，并指引 `/resume` 或 `/new`；不实现自动合并。`tests/test_session_store.py`
   使用两个独立 spawn 进程验证文件锁与 revision，结果为一次保存、一次明确冲突；
   WSL Session 定向测试 11 passed，CLI 加载节点 2 passed。
5. **H-独立验收（已完成）**：`AcceptanceContract` 已支持必改/禁改路径和 Runner
   持有的隐藏 verifier；`RunResult.status`、`acceptance_status`、`delivery_status` 分别
   表示 Agent、独立验收和交付状态。普通运行只比较契约文件的运行前后内容指纹，避免
   把用户已有修改误算为 Agent 改动；隔离运行复用 worktree `changed_files`。隐藏 verifier
   不进入 History。实际修改 `agent/runner.py`、`agent/task.py`、`tests/test_runner.py`；
   最终 WSL Runner 测试 4 passed，EvalRunner 兼容回归 3 passed。
6. **H-交付闭环（已完成）**：自动 PR 现在要求显式 `--verify-command`，且只有 Agent
   成功、Runner 独立验收通过后才 commit、push、create PR；自动交付拒绝运行前已有修改，
   无 diff、验收失败、commit/push/PR 失败分别写入 `delivery_status`。push 失败保留本地
   commit，PR 失败后的同进程重试通过本地/远端 SHA 跳过重复 commit/push。实际修改
   `entry/github_issue.py`，新增 `tests/test_github_issue_delivery.py`；本地 bare remote 与
   fake GitHub client 契约测试 4 passed，既有 GitHub Issue 逻辑回归 3 passed，未访问
   真实 GitHub，也未提取新的交付模块。
7. **H-证据包（首个真实案例已完成）**：在 `Napabana/pr-test` 冻结 Issue #4，使用仓库外
   verifier 完成一次基线失败、三次 Agent 失败和一次完整成功链路；真实 PR #5 指向 commit
   `0c5b108`。案例同时发现并修复 GitHub Issue registry 未绑定目标 cwd、Agent 可在独立
   验收前自行 commit 两个缺口。成功运行 8 steps、47,454 tokens、40.3s，目标仓库 pytest
   15 passed，独立验收 passed。上述数字只描述该次运行，不计算成功率或性能收益。主调用链、
   失败分类和 90 秒 case study 见 `../../changes/2026-09-15/pr-test真实PR改动内容.md`；Repo Map/Compaction
   消融、完整隔离图和 Claim 账本仍待后续真实数据。

### 12.3 真实 PR 合并后的优先级

`Napabana/pr-test` PR #5 已于 2026-09-15 12:19:50（北京时间）合并，Merge Commit 和
`main` HEAD 均为 `f5ad77c739e21efcd65e6e6524f320e3325a96f7`，Issue #4 已关闭。后续按以下顺序执行：

1. **Token-safe clone（已完成）**：`clone_repo()`、push 与远端 SHA 查询通过 Git 临时
   `http.extraHeader` 环境认证，HTTPS URL 与 remote 不含凭据；Git 子进程不继承明文
   `GITHUB_TOKEN`，并移除可能打印 HTTP 头的 trace 开关。标准库契约测试覆盖成功与失败
   输出；真实私有 `pr-test` clone 成功，origin 为无凭据 URL，未引入依赖。
2. **`--no-pr` 兼容契约（已完成）**：现有 registry 契约已参数化，证明只有自动 PR
   模式移除 `git_add`/`git_commit`，`--no-pr` 保持原有工具能力。WSL 结果 2 passed；
   生产行为符合预期，因此未修改生产代码。
3. **失败样本归档与最小恢复评估（首轮已完成）**：扫描现有 22 个 JSONL，provider 空
   响应命中 1 个 Run 且终态失败；`finish` 误调用命中 3 个 Run，其中 2 个下一步恢复并
   完成，另 1 个因先前 test cwd 问题最终失败。任务与运行条件不统一，因此这些计数不
   表述为失败率。当前不修改恢复代码、不扩展 multi-tool call 或工具协议，后续固定任务
   继续积累同分类样本。
4. **真实消融与证据补齐**：依次完成 Repo Map、Context Compaction 消融，再补
   Worktree/Docker/Permission 隔离图和 Claim 账本；所有数字必须能从原始 Trace 重算。
5. **auto-merge 后置**：真实项目继续人工审阅合并。只有专用测试仓库出现明确需求，且
   分支保护、CI、最新 HEAD 与重复请求语义均固定后，才单独设计可选 auto-merge。

Token-safe clone 实际修改 `entry/github_issue.py` 与 `tests/test_github_issue_delivery.py`；
新增安全节点 2 passed，受影响 push/retry 回归 2 passed，最终收紧子进程环境后对应节点
1 passed，真实私有 clone 冒烟成功。第 2 项兼容契约随后以 2 passed 完成。下一批进入
第 3 项首轮统计也已完成并决定不写恢复代码。下一批进入第 4 项真实消融，先冻结实验
Commit、任务集、模式和原始输出目录，再决定最小脚本/文档范围；不得顺带实现模型协议、
auto-merge 或新依赖。

P1 第一轮收口以第 1～6 项为代码完成边界；第 7 项必须建立在真实执行结果上，不能用
单元测试数量代替。MCP、多 Agent、multi-tool call、树形 Session 和向量检索继续后置。

## 13. 2026-09-16 B2 termination 收口与 v3 证据

B2 已完成最小 termination 语义闭环，不扩展为完整 Resource Manager。`FINISH` 被 guard
拒绝后可恢复继续，hard ceiling、框架 loop、Provider/基础设施失败、显式 GIVE_UP 与取消
分别记录；中性资源提示不进入 canonical History。固定 3 case × 3 variant × 1 run 已完成，
原始结果与 v2 对照位于 `evals/results/context_policy_agent_ablation_v3/`。

结果仅作为本次 9-run 描述性证据：baseline/pruning/hybrid strict pass@1 分别为 1/3、
2/3、2/3，verifier pass 分别为 2/3、2/3、3/3。样本量与模型非确定性不足以支持泛化收益
主张。B2 后停止 termination/resource 扩张，下一批仍按 TODO 的 P0-2/P0-3 尾项、P0-1
少量缺口、P1-2、P1-4、P1-5、P1-6 顺序由用户另行授权。
