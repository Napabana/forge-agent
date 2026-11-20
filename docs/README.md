# Forge Agent 文档

Forge Agent 面向可验收、可追踪和可恢复的编码任务。这里介绍当前实现的模块职责、设计理由、使用方法和预期效果。

## 按需求阅读

- [整体架构](design/architecture.md) · CLI 和评测共用执行层，形成可观察、可验收、可续接的任务。
- [任务执行](design/execution.md) · ExecutionRunner 为入口提供统一生命周期，使同一个任务在交互、自动化和评测中使用同一套 Agent 与工具。
- [Agent 与工具执行](design/agent-tools.md) · 模型擅长选择下一步，工具执行层负责核验实际动作的执行条件和结果。
- [上下文与仓库导航](design/context.md) · 长任务的模型窗口有限。分层上下文让模型优先看到目标、近期观察和相关源码，同时保留可回查的原始证据。
- [计划与执行恢复](design/planning-recovery.md) · 计划让多步骤工作有明确状态，失败恢复把工具错误、语义停滞和基础设施故障转化为可检查的后续决策。
- [Skills 与 MCP](design/extensions.md) · Skills 提供按需载入的工作方法，MCP 适配外部工具，使扩展能进入统一调用链。
- [隔离、验收与 GitHub 交付](design/isolation-acceptance.md) · 隔离执行为变更提供独立工作目录，验收判断变更是否满足要求，交付把已验证成果发布到远端。
- [评测与经验迭代](design/evaluation.md) · 评测把模型行为与独立评分放入可复现实验。经验模块把通过验证的轨迹加工成候选知识。
- [Supervisor 与 Worker](design/workers.md) · 主 Agent 可以把仓库探索、测试分析和审查并行分派，并在同一个任务里汇总发现。
- [项目记忆](design/project-memory.md) · 跨任务保留经过验证、可归属到仓库的项目知识，减少重复探索。
- [长时运行与恢复](design/durable-runtime.md) · 进程中断后复用已提交的动作和结果，并为无法确认的外部副作用保留核查路径。
- [配置、模型与用量](design/configuration.md) · 配置层把用户设置与模型、工具和运行时能力连接起来，Provider 适配器统一协议与用量语义。
- [模块与源码索引](design/source-index.md)
- [源码阅读路线](source-navigation.md)
- [命令查阅](reference.md)

## 维护

[当前项目状态](PROJECT_STATE.md)记录公开能力和验证范围。[文档维护](design/MAINTENANCE.md)说明本地构建和一致性检查。
