# 阶段 06：Planning、Failure-aware Recovery、Loop/Progress 与消融评估

> 2026-11-14｜分支 `docs/qwen38-27b-pilot-r1`；生产源码基线 `ca396ce6f14b88dd439bebdcb3b9634645017034`。覆盖 E01–E11，共 11 项。**静态审查 + 已归档历史 Benchmark 数据核查**；未运行新模型或 Python 全量回归。

## 1. 当前流程

`agent/planning.py::decide_planning` 决定 off/auto/always；`PlanningRuntime` 持有 ExecutionPlan/step/version，向模型暴露控制 Tool。Agent 执行后 `agent/loop_detector.py` 判重复动作，`SemanticProgressTracker` 判进展，`agent/recovery.py::FailureContext` 和 `RecoveryPolicy` 针对失败类别选 inspect/retry/replan/change_approach/rerun_test；超过类别或总预算退出。需 replan 时由 `RecoveryRuntime` 限制额外代码修改，不能代替 Completion Guard。

## 2. 十一项源码审查

| ID | 能力 | 证据路径 | 当前实现与主要风险 | 静态暂评分 |
|---|---|---|---|---|
| E01 | Planning off/auto/always | `agent/planning.py::decide_planning` | auto 基于 require_tests/路径提示/英文复杂词触发；简单多步任务可能判错 | 7/10（中） |
| E02 | ExecutionPlan state | `agent/planning.py::ExecutionPlan/PlanStep` | typed 状态、版本/ID、terminal 步骤不可回滚 | 7.5/10（中） |
| E03 | 动态控制工具 | `agent/planning.py::planning_control_schemas/PlanningRuntime.apply_control` | plan_create/update/revise 的 schema 随状态变化 | 7/10（中） |
| E04 | mutation gate | `agent/core.py + agent/planning.py` | 计划创建前阻挡有副作用操作、计划版本 gate | 7/10（中） |
| E05 | compaction 后计划 | `agent/planning.py::PlanningRuntime.render_context` | 计划独立内存保留，下一轮重建提示 | 7.5/10（中） |
| E06 | FailureContext | `agent/recovery.py::FailureContext/classify_tool_failure` | 分类测试失败/权限/infra/loop/completion 等 | 7/10（中） |
| E07 | RecoveryPolicy | `agent/recovery.py::RecoveryPolicy._choose_strategy` | inspect/retry/replan/change_approach/rerun_test 条件分支 | 7/10（中） |
| E08 | Recovery budget | `agent/recovery.py::RecoveryPolicy.decide` | 类别次数和 global ceiling；infra 默认 fatal | 7.5/10（中） |
| E09 | Replan gate | `agent/recovery.py::RecoveryRuntime.requires_replan` | pending revision 下阻止进一步仓库修改，FINISH 保留 Guard | 7/10（中） |
| E10 | 触发语义 | `agent/core.py::Agent.run; agent/loop_detector.py` | Loop Detector、语义无进展、Test Tool 失败产生恢复路径 | 6/10（高） |
| E11 | 模型效果/成本 | `evals/results/benchmark_v1_r2_*/report.json` | R2 24+24 runs，两变体均 23/24，Full P2 更耗 token/steps | 5/10（高） |

评分按设计2、正确性3、安全可靠性2、证据2、扩展1，暂评分不是动态实验优劣排序。

## 3. 可复核的历史 Benchmark V1 R2

固定输入来自仓库中的：
- `evals/results/benchmark_v1_r2_baseline_real/report.json` 与 `metadata.json`
- `evals/results/benchmark_v1_r2_full_p2_real/report.json` 与 `metadata.json`

两组均使用 `Napabana/pr-test@23019998f2e801e79dea59fd23fc49c58fc20038`，12 tasks × 2 repetitions，各 24 runs；模型为 `deepseek-v4.1-flash`，相同 suite SHA、max_steps=30、budget_tokens=60000。区别是 Baseline planning/recovery/skills 全部 off；Full P2 为 planning=always、recovery=structured、skills=true。**多个变量一起变化，不是单模块消融。**

| 指标 | Baseline React | Full P2 |
|---|---:|---:|
| 观察成功数 | 23/24 | 23/24 |
| 成功率（仅此历史样本） | 95.83% | 95.83% |
| 平均步数 | 12.375 | 15.875 |
| 平均 total tokens | 172,758 | 227,781 |
| 平均 Wall time | 102.58s | 119.89s |
| 平均工具调用 | 11.375 | 11.083 |
| 平均 Recovery selected | 0 | 0.833 |

Full P2 相对 Baseline 每 Trial 平均多 55,023 Tokens（约 +31.9%）、多 3.5 步、多 17.3 秒，观察成功率没有提升；**不能证明 Planning/Recovery/Skills 单独无价值**，只能说联合变体在这个小样本上的成本回报暂不成立。

## 4. 开源实现对照

- [Pi Agent turn loop](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L173-L310) 核心职责是逐轮响应/工具/继续或结束；Forge 的 typed plan 与 Recovery Policy 是额外宿主控制机制。不可宣称 Pi 一定没有上层规划扩展。
- [Codex turn loop](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/session/turn.rs#L642-L860) 按模型继续需求和工具返回决定后续处理、运行 Stop Hook；对照价值是把“模型主循环、宿主状态、停止审核”分层，并非借用其未证实的规划算法。
- [Claude Code 官方 Hooks](https://code.claude.com/docs/en/hooks) 说明模型调用前后和 Stop 阶段可扩展控制；不推断 Claude Code 内部独有的 Recovery Policy。

## 5. 需要反证的状态行为

| 用例 | 输入 | 预期 |
|---|---|---|
| PL06-01 | `auto` + 中文多文件任务或无路径但复杂任务 | 检查启用决策是否错误；不要为适配测试直接增加硬编码中文关键字 |
| PL06-02 | mutation 早于 plan_create | 不可绕开 mutation gate；只读应继续允许 |
| PL06-03 | plan_create→replan→compact→resume | plan version/step_id 与真正状态一致 |
| REC06-01 | TEST_FAILURE → 检查→再次失败 | 按分类和预算选择恢复，但不要形成无界 Tool Call |
| REC06-02 | 权限拒绝、MCP 超时、infra failure | 区分可改方法、重试一次与 fatal；不重试相同被禁指令 |
| REC06-03 | 测试通过后只读循环 N 次 | 对照 `agent/core.py::_apply_no_progress_guard`：目前成功测试快照可能持续清零 no-progress，需宿主 positive stop 提示 |
| AB06-01 | 固定模型/任务/环境，分别独立打开 planning / recovery / skills | 记录 paired success delta、token delta、step、walltime、provenance；设费用上限 |

关联测试：`tests/test_structured_planning.py`、`test_structured_recovery.py`、`test_loop_detector.py`、`test_failure_harness.py`、`test_coding_agent_eval.py`。**本轮未执行。**

## 6. 决策与优先级

**P1**：和阶段 08 一起建立 `completion_candidate/wrap_up_required`，避免模型在已验证修复后无节制地 Recovery/inspect。需离线 S08/REC06 反例；不要直接以任意 PASS 自动 FINISH。

**P1**：将执行步骤、模型调用、Token、时间等累计预算加入每 Trial 合同，避免组合控制引发额外成本（1–3 人日方案/离线验证）。

**P2**：基于难度/失败征兆而非默认 always 选择 Planning/Recovery；必须先完成独立变体消融（需要额外 API，当前不执行）。

**P2**：改进 auto 触发的任务语言覆盖，在已有测试中先比较漏判率，不立刻修改关键词。

面试问：为什么有 Planning 反而更贵？答：Typed Plan 控制状态和 Recovery 动作会引入附加 Tool/LLM 轮次。历史 R2 联合变体成功率同为 23/24，但平均 Tokens 由 172,758 上升至 227,781；这仅能说明当前联合默认策略在该任务集上没有观察到净收益。

**证据边界**：已完成 E01–E11 源码/历史证据比对和改进矩阵；没有证明任何新的策略在真实任务上改进成功率。下一阶段 07 审查实际隔离与无人值守权限。
