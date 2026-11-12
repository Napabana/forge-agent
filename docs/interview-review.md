# 面试复习入口

## 30 秒项目介绍

Forge Agent 是一个以同步 ReAct 主循环为内核、由统一 Runner 服务 CLI、Chat、API 和 GitHub Issue
入口的编码 Agent。项目把 Context Engineering、可审计 Tool 生命周期、typed Planning/Recovery、
Skills/MCP、Worktree 隔离、独立验收和冻结 Evaluation Harness 组织为一条可追踪的工程闭环；运行后
演化保持离线，Candidate 只有经过显式 PromotionGate 才能进入正式 Skill。

## 五分钟复习顺序

1. 用[系统架构图](design/Forge-Agent-当前代码架构.md#2-system-architecture)说明组件边界。
2. 用[任务生命周期](design/Forge-Agent-当前代码架构.md#4-task-execution-lifecycle)说明 direct/isolate 分叉。
3. 用[Agent 与 Tool Lifecycle](design/02-Agent与Tool生命周期.md)说明安全与完成判定。
4. 用[Context Engineering](design/03-Context与RepoMap.md)说明长上下文和仓库导航。
5. 用[Planning/Recovery](design/04-Planning与Recovery.md)与[评测演化](design/07-Evaluation与Evolution.md)说明设计取舍和证据边界。

## 核心问题入口

每个专题末尾已有“面试问题”及答案，本页不复制第二份答案：

- 为什么所有入口共享 Runner，但只有 isolate 进入 Orchestrator？见[任务执行主链](design/01-任务执行主链.md)。
- Tool Call 为什么必须经过 validation、hooks、permission 和 constraints？见[Agent 与 Tool](design/02-Agent与Tool生命周期.md)。
- Repo Map、Token Budget 与 Compaction 各解决什么问题？见[Context](design/03-Context与RepoMap.md)。
- 为什么 Planning 和 Recovery 是 typed runtime state，而不是只靠 Prompt？见[Planning/Recovery](design/04-Planning与Recovery.md)。
- Skill progressive disclosure 与 MCP 外部进程分别有哪些信任边界？见[Skills/MCP](design/05-Skills与MCP.md)。
- Worktree 隔离、只读约束、独立验收分别保证什么？见[隔离与验收](design/06-隔离与独立验收.md)。
- 为什么冻结 Benchmark 和 PromotionGate 不能证明线上总体能力？见[Evaluation/Evolution](design/07-Evaluation与Evolution.md)。

## 设计取舍与实验结果

- 先复述专题的“设计取舍”，再用同页“测试与证据”限定结论。
- Benchmark V1 R2 中 Full P2 与 baseline observed acceptance 均为 23/24；Full P2 成本更高，因此不能包装成成功率提升。
- solver、fixture、单次真实案例和离线重评分只支持各自协议内的结论。
- 需要追溯原始证据时进入[参考资料](reference.md)，不要把历史日志当作当前实现。
