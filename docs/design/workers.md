# Supervisor 与 Worker

## 为什么实现

主 Agent 可以把能够独立交付的仓库探索、测试分析和审查交给 Worker。Run、Chat 轮次和评测复用同一策略。派发时给出一个问题、所需证据和结束条件。主 Agent 可以直接处理小任务，也可以在后台 Worker 执行期间推进自己的步骤。

## 怎么使用

```bash
forgeAgent --multi-agent --worker-mode background run --repo . \
  --task '让 Worker 审查当前 diff，同时由主 Agent 运行回归，需要时读取审查结果'
forgeAgent --multi-agent --worker-mode sync run --repo . \
  --task '让 Worker 审查当前 diff，同时由主 Agent 运行回归，需要时读取审查结果'
```

省略命令行开关时使用 `agent.multi_agent_enabled` 和 `agent.worker_mode`。多 Agent 默认关闭，Worker 模式默认 `sync`。在 Chat 中也可使用同一组全局开关。主 Agent 自行决定是否派发及选择 `explorer`、`test_analyst` 或 `reviewer`。

## 怎样实现

`AgentCoordinator` 根据角色选择工具、模型和预算。主 Agent 能看到每个角色的上限和池余额。配置 `max_total_tokens` 时，Worker 池最多占运行额度的三分之一，并发派发预留用量，未使用的额度在结算后仍可使用。未派发时，主 Agent 可以使用完整额度。Worker 每轮看到自己的累计额度和剩余量，请求前估算输入和输出成本。接近限额时可以用一次短请求收尾。

| 后台工具 | 行为 |
| --- | --- |
| `delegate_tasks` | 后台模式立即返回 Worker ID、队列状态和实际额度。同步模式等待整批结果。 |
| `worker_status` | 查询单个或全部 Worker 的状态、最近动作和累计用量。 |
| `worker_wait` | 按 ID 领取结果，可以限定等待时间。设为 0 即时查看；达到时限时返回当前状态。 |

Worker 完成即交付。额度不足、超时或中断时，结果包含停止原因和最近保存的工具证据。后台结果摘要在主 Agent 下一次正常决策时送入模型请求，完整结果按需领取。Trace 用 `worker_partial_ready` 和 `worker_finished` 记录可用结果，用 `worker_result_delivered` 记录摘要进入了收到响应的模型请求，用 `worker_result_read` 记录显式领取。背景任务持久化时在现有 SQLite 出站事件表保存派发、工作区快照和结果，恢复按任务 ID 复用结果、用量和已经交付的摘要。

## 预期效果

主 Agent 的动作可以出现在 Worker 开始和结束之间。长时运行 Benchmark 对照单 Agent、同步与后台 Worker，记录逐 Trial 的独立验收、总耗时、用量、派发和等待耗时、重叠时间，以及主模型是否收到并使用结果。

## 使用边界

Worker 执行只读工作，Lead 持有变更与决策责任。`worker_wait` 的 `workspace_changed` 提示结果是否需要按当前工作区复核。超时可先送回已观察到的证据，正在请求模型的线程仍会在安全边界后释放。累计 token 的请求前估算依赖模型计数器，强制的物理额度仍由支持可信计数的请求预算执行。Benchmark 的固定脚本用于核验机制，真实模型的效率收益需要配对运行。
