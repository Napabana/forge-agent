# 2026-09-15 Context Compaction 规划收口

## 本轮目标

在开始正式 Context Compaction benchmark 前，先审计当前设计是否存在职责冲突，并将优化方案、实施批次、详细 TODO 和测试策略固化到仓库，避免后续上下文丢失后重新讨论。

本轮不修改任何生产 Python 代码，不运行真实模型，不产生 Context Compaction 性能或成功率结论。

## 审计基线

分析时远端 `dev` 为：

```text
a86c4e48f3cd4235866371d8f534dc346e93c905 完善repomap对比测试
```

现有 Repo Map benchmark 已形成可重算数据，本轮不改 Repo Map 实现或实验结果。

## 当前 Compaction 结论

当前实现已有好的基础：

- `TraceableCompaction` 通过 `prepare_next_turn` 接入，不把压缩分支硬写进 `Agent.run()`。
- 原始 Tool 结果保留在 EventLog，压缩 checkpoint 保存 source event IDs。
- checkpoint 已记录 before/after tokens、summary hash 和 repo revision。
- Session 能持久化 compaction checkpoints。

但存在必须在正式 benchmark 前先收口的职责冲突：

1. `ConversationHistory` 按消息数 destructive trim，可能在 Compaction 前永久删除旧历史。
2. History、Compaction、TokenBudget 对 Action/Observation unit 的定义不一致。
3. Compaction 只基于 history 子预算触发，而不反映完整 request pressure。
4. `extractive-v1` 本质是 transcript 串接 + 字符截断，不是真正的结构化语义摘要。
5. summary 直接作为普通 user message 写回 History，重复压缩有 summary-of-summary 漂移风险。
6. Compaction checkpoint 只用 Git HEAD，而 Chat Repo Map 已考虑 working tree，repo revision 语义不统一。
7. `event_ref` 当前支持人工审计追溯，但没有 Agent 可调用的 context recall 工具。
8. `/resume` 或新 Chat round 的 step 1 不执行 `prepare_next_turn`，长 Session 第一轮只能依赖 TokenBudget trim。

因此当前版本适合作为 deterministic baseline，不适合作为最终设计直接做正式收益 Claim。

## 目标设计

将 Context 拆成三层：

```text
Canonical State
  Session / EventLog / Repository
        |
        v
Context State
  Anchors / CompactionEntry / Recent History Units / event refs
        |
        v
Model-visible View
  System / Repo Map / Constraints / Summary / Recent Turns / Retrieved Context
```

核心原则：Compaction 不再修改事实本身，只修改下一次模型可见 Context View。

具体方案见：

- `CONTEXT_COMPACTION_EXECUTION_PLAN.md`
- `CONTEXT_COMPACTION_TODO.md`

## 后续实施顺序

严格按以下批次推进，每批开始生产代码修改前都重新按本地 `AGENTS.md` 列文件和理由并等待用户确认：

1. C1：Context 所有权 + 统一 HistoryUnit + token-based recent tail。
2. C2：完整 Request Pressure + 独立 CompactionEntry。
3. C3：统一 RepositoryState + Session preflight。
4. C4：Deterministic Tool-output pruning。
5. C5：Structured Compaction。
6. C6：只有 benchmark 证明必要时才做 `context_recall(event_ref)`。
7. B1：完全离线的 Context Policy benchmark。
8. B2：基于固定 prerecorded history 的真实 Agent 三组消融。

## 测试原则

先验证 Context policy 正确性，再验证模型任务效果，避免把两类问题混在一个 benchmark 中。

离线阶段不调用真实模型，至少覆盖：

- early hard constraint；
- huge Tool output；
- Action/Observation pair；
- superseded state；
- repeated compaction；
- resume long session；
- dirty repo revision。

真实 Agent 阶段主对比：

```text
A. budget_trim_only
B. deterministic_pruning
C. hybrid_compaction
```

旧 `message_window_40` 仅作为 legacy 辅助对照。

主指标只有：

1. Hidden verifier pass rate。
2. Provider input tokens per solved task。

辅助同时记录 cache hit、latency、tool calls、false-finish、context pressure 和 compaction ratio。

## 本轮修改文件

仅新增规划文档：

- `CONTEXT_COMPACTION_EXECUTION_PLAN.md`
- `CONTEXT_COMPACTION_TODO.md`
- `2026-09-15-Context-Compaction规划收口.md`

未修改：

- `context/compaction.py`
- `context/history.py`
- `context/token_budget.py`
- `agent/core.py`
- `entry/chat.py`
- `agent/session.py`
- `config/default.yaml`
- eval benchmark 代码

## 测试

本轮只有文档变更，因此未运行 pytest，也未调用真实模型。

## 下一步确认点

下一批只允许执行 C1，预计修改：

- `context/history.py`：停止 Compaction 前的 destructive message-count 抢先裁剪，同时保持旧调用兼容。
- `context/token_budget.py`：提取统一 Action/Observation HistoryUnit 语义供 Context 策略复用。
- `context/compaction.py`：复用统一 HistoryUnit，并把 recent tail 改为 token-based；暂时保留 `extractive-v1`，不引入 LLM summary。
- `tests/test_compaction.py`：覆盖 canonical history、不产生 orphan、token-based tail 和默认行为兼容。

用户明确确认 C1 文件范围后才开始实现。
