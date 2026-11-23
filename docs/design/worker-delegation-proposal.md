# Forge Agent 的 Worker 协作设计

本文记录共享委派策略。当前行为和使用方法见 [Supervisor 与 Worker](workers.md)。

## 适用范围

这套规则属于 Forge 的共享执行层。CLI 的 Run、Chat 每轮和评测 Trial 都通过同一套机制决定是否委派、怎么分预算和怎么收结果。适用于代码探索、测试分析和审查等工作。启用多 Agent 后，主 Agent 仍可自行完成整个任务。Worker 的结果、用量和恢复状态归属发起它的 Run 或 Chat 轮次。

## 协作规则

1. **主 Agent 决定是否委派。** 把边界清楚、结果会影响后续决策的工作交给 Worker。派发时说明问题、交付物与完成条件，主 Agent 继续自己的工作，并负责整合结果和最终验收。
2. **运行时分配和结算预算。** 运行预算计入主 Agent 和所有 Worker 的用量，并给主 Agent 留出执行和验收的额度。主 Agent 派发前能看到角色上限和池余额，派发后知道实际分配。Worker 每轮能看到自己的剩余额度。运行时在发请求前估算输入和输出成本，留出收尾空间。
3. **Worker 在预算内交付。** 证据足以回答问题就立即提交。达到收尾阈值、超时或中断时，运行时先保存并推送已有证据、停止原因和待核实事项。Worker 来不及总结时，运行时用已保存的工具观察交回可核对的进展。
4. **异步送达，按需等待。** 派发立即返回。主 Agent 到自然决策点时能看到已到达的摘要，需要完整结果时用 `worker_wait` 按 ID 领取。下一步确实依赖结果时才等待。同步模式复用同一套预算和交付规则，方便对照。

## 提示词

**主 Agent 派发说明**

> 只有当独立工作能给主任务带来可用结果时才派发。写清要回答的问题、已有线索和期望交付的证据。根据实际额度缩小范围，派发后继续做自己的工作。

**Worker 共用指令，附在现有角色说明后**

> 围绕当前问题取证。先搜索相关位置，再按需读取。证据足以回答问题时，立即提交结论、证据位置和建议的下一步。预算不足时交回已确认的事实，以及还没有核实的问题。

## 怎么验收

- **回归**覆盖不派发、预算内交付、触顶和超时后的部分交付、恢复时通知去重。EasySudoku 的 23,905 / 24,000 tokens 轨迹可用于重现触顶场景。
- **Trace**核对三个独立事实。Worker 运行期间主 Agent 做了别的事，Worker 摘要实际进入主模型请求，主 Agent 的后续修改或测试用到了结果。
- **Benchmark**用小改动、代码探索和独立审查等不同任务，对照单 Agent、同步 Worker 和后台 Worker。同一任务保持模型、总预算和独立验收一致，逐 Trial 记录通过情况、总时间、总 token、实际等待和结果使用。

异步交付参考 DeepSeek Harness，任务范围和角色提示词参考 Claude Code 和 Pi Agent。 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent/README.md#managed-delegation) · [Claude Code](https://code.claude.com/docs/en/sub-agents#choose-between-subagents-and-main-conversation) · [Pi Agent](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/subagent/README.md)

预算收尾与推送由运行时负责。实际收益以对照 Benchmark 为准。
