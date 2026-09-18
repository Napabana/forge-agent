# P2-0 Coding Agent Evaluation Harness 本地回归验证收口

日期：2026-09-18  
实现提交：`36aa0895730ca4145947e3bee4f35779dc5563ae`  
状态：**DONE**

## 验证事实

P2-0 实现提交时，ChatGPT 执行环境无法取得可执行仓库 checkout，因此只完成了静态编译与 schema smoke，并将状态保留为 `IMPLEMENTED / LOCAL VALIDATION PENDING`。

用户随后在本地按交接要求完成验证，并明确确认全部通过，包括：

- `tests/test_coding_agent_eval.py` 新增专项测试；
- Runner / Failure Harness / Trace / Acceptance / Evidence Pack 等相关回归；
- `python -m evals.verify_evidence_pack`；
- 全量 `python -m pytest -q`。

用户未提供具体 passed 数量、完整 stdout 或耗时，因此本文不补造数量和性能数字。

## 状态收口

基于用户实际完成的本地验证：

```text
P2-0
IMPLEMENTED / LOCAL VALIDATION PENDING
        ↓
DONE
```

确认的范围是 Evaluation Harness 自身和与现有生产执行主链的 deterministic compatibility：

```text
EvaluationSuite
  ↓
EvalTask / Trial
  ↓
ExecutionRunner → Agent → ToolExecutor
  ↓
RunResult + Trace
  ↓
Deterministic Graders
  ↓
TrialResult / EvalReport
```

## Evidence boundary

本次 pytest / regression 通过不改变真实模型证据状态。

`evals/results/coding_agent_baseline_not_executed/` 继续保持：

```text
variant = baseline_react
execution_status = not_executed
real_model_executed = false
```

因此当前可以证明：

- Coding Agent Evaluation Harness 已实现；
- 8-case fixture/reference validation 与 deterministic regression 可运行；
- Harness 复用生产 `ExecutionRunner → Agent → ToolExecutor` 主链；
- no-overwrite、not-executed、fake/scripted evidence boundary 等 contract 已通过本地回归。

当前仍不能宣称：

- Forge Agent 总体 success rate；
- baseline_react pass@1；
- Planning 相对 baseline 的成功率提升；
- 真实模型 token / latency 优势。

这些必须由后续显式 real-model experiment 提供。

## 下一步

进入 **P2-1 Structured Planning**。

P2-1 必须继续复用 P2-0：

```text
baseline_react
vs
planning
```

使用相同 suite / trial / repetition / grader / report 协议做消融，不新建独立评测链。
