# Supervisor 与 Worker

## 为什么实现

主 Agent 可以把仓库探索、测试分析和审查并行分派，并在同一个任务里汇总发现。

## 怎么使用

用 `forgeAgent --multi-agent run --repo . --task '检查代码，按需委派分析'` 启用委派能力。Worker 角色和模型可在配置中查询。

## 怎样实现

`DelegateTasksTool` 接收结构化子任务，`AgentCoordinator` 根据角色选择工具、模型和预算，调度并归集结果。子 Run 具有独立 ID，持久化时共享 Lead Scope 与 Owner Epoch。续接可复用已提交的子结果。

## 预期效果

并行获取相互独立的分析，记录每个 Worker 的来源、用量和完成情况。

## 使用边界

Worker 默认执行只读工作，Lead 持有变更与决策责任。已发出的同步请求只能在调用边界响应取消。
