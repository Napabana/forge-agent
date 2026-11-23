# 模块与源码索引

按职责找到实现入口。每个源码目录的 README 补充了使用方式与主要文件。

| 目录 | 职责 | 设计 |
| --- | --- | --- |
| `agent/` | 将任务编排为模型调用、工具观察和独立验收，分配 Worker 预算并交付结果。 | [Agent 执行](execution.md) · [Worker](workers.md) |
| `config/` | 将运行配置、模型档案、预算和系统凭据引用加载为类型化对象。 | [配置与凭据](configuration.md) |
| `context/` | 维护完整历史、模型视图、上下文预算、RepoMap 与其仓库状态，以及归档来源。 | [上下文与仓库导航](context.md) |
| `durable_runtime/` | 管理 SQLite、Scope Owner、检查点、工具日志和未知副作用核对。 | [持久化运行时](durable-runtime.md) |
| `entry/` | 将终端命令、Chat 会话和 GitHub Issue 转成 Runner 可执行任务，并维护 Chat 进程的审批选择。 | [命令行任务执行](execution.md) · [终端 UI 与事件展示](terminal-ui.md) |
| `evals/` | 运行编码任务、上下文实验和故障注入，记录独立 grader、Trace 及用量。 | [评测](evaluation.md) |
| `experience/` | 从运行轨迹提取候选经验，对候选进行受控评测并决定是否晋升。 | [经验候选](evaluation.md) |
| `harness/` | 实施 Hook、权限、只读约束和取消边界，记录工具调用与执行结果。 | [工具执行控制](agent-tools.md) |
| `ipc/` | 为编排器和观察者提供进程内消息分发。 | [进程内事件总线](execution.md) |
| `llm/` | 统一模型消息、工具 schema、流式输出、预算、重试分类和用量。 | [模型适配与计量](configuration.md) |
| `mcp_integration/` | 连接 MCP server，将发现的工具转换为 Forge 工具。 | [MCP 集成](extensions.md) |
| `project_memory/` | 分项目检索可复用知识，保存候选、来源与验收关系。 | [项目记忆](project-memory.md) |
| `runtime/` | 管理隔离 Git worktree 的创建、续接、检查和清理。 | [工作树运行时](isolation-acceptance.md) |
| `skills/` | 发现受信任 Skill，按需加载正文和资源，向模型暴露可用索引。 | [Skill 发现与加载](extensions.md) |
| `task/` | 用持久队列记录任务认领、执行状态、父子关系和工作树绑定。 | [任务状态与认领](execution.md) |
| `tools/` | 提供文件和符号导航、执行命令与测试、区分工具状态及测试结论，并管理 Git、运行容器和归档读取。 | [内置工具](agent-tools.md) |
| `scripts/` | 提供文档校验、任务与评测辅助入口。 | [开发与评测脚本](evaluation.md) |
| `tests/` | 覆盖模型适配、Agent 工具、Trace、持久化和外部接口的本地回归。 | [测试](evaluation.md) |
| `examples/` | 展示如何把 Forge Agent 接到 Harbor 等评测流程。 | [集成示例](evaluation.md) |
