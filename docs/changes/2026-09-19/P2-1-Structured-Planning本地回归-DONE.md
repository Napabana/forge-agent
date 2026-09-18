# P2-1 Structured Planning 本地回归验证收口

日期：2026-09-19  
最终修复基线：`dev@6ab65106debe8d685e5b8940f3c9cf654cbef4f9`  
状态：**DONE**

## 验证事实

P2-1 初始实现后，用户在本地连续执行定向与全量 regression。过程中真实暴露并修复了三类问题：

1. Trace v2 exact-key contract 未同步新增 `planning_tokens`，同时发现 `estimated_input_tokens` 漏加该拆分项；
2. `planning_mode=off` 的空 Planning context 被通用 estimator 保守估算为 1 token，导致 diagnostic attribution 不准确；
3. `config/default.yaml` 的裸 `planning_mode: off` 被 PyYAML 按 YAML 1.1 解析为 `False`，导致 Chat / CLI / isolate / default config 入口共同失败。

对应修复后，用户再次执行本地全量 `python -m pytest -q` 并明确确认全部通过。

最终通过轮次未提供完整 stdout、passed 数量或耗时，因此本文不补造具体数量。

## 状态收口

```text
P2-1
IMPLEMENTED / LOCAL VALIDATION PENDING
        ↓
DONE
```

确定性验证覆盖的实现主链：

```text
planning_mode
  ↓
PlanningDecision
  ↓
ExecutionPlan / PlanStep / PlanRevision
  ↓
Agent.run internal plan controls
  ↓
current plan runtime context
  ↓
ToolExecutor / Completion Guard
  ↓
Trace v2 + Evaluation Harness metrics
```

同时回归了 Planning 与既有 Runner、Context Compaction、Trace、Failure Harness、Evidence Pack 和产品入口的兼容。

## Evidence boundary

本次本地 pytest 通过能够支持：

- Structured Planning 已实现并进入生产 Agent 主循环；
- `off | auto | always` 配置与产品入口接线可用；
- typed Plan / Step / Revision 生命周期可观测；
- current plan 在 compaction/history override 下仍保留；
- planning lifecycle events 与 token diagnostics 已进入 Trace v2；
- P2-0 `planning` variant 确实启用该 architecture，而不是仅修改 metadata。

本次验证不能支持：

- Planning 提高真实模型 coding success rate；
- Planning 提高 pass@1；
- Planning 降低 token 或 latency；
- Planning 对 multi-file task 有统计显著提升。

原因是本阶段仍未执行正式 real-model `baseline_react vs planning` A/B。

## 下一步

进入 **P2-2 Failure-aware Recovery + Replanning**。

P2-2 应直接复用 P2-1 的 current plan / current step / `plan_revise` mechanism，不建立第二套 Agent loop，也不把 infrastructure/cancel/provider transport retry 包装成“智能恢复”。
