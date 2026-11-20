# 评测

运行编码任务、上下文实验和故障注入，记录独立 grader、Trace 及用量。

## 怎么使用

使用 `python -m evals.coding_agent --help` 选择任务，或运行 `python -m evals.long_horizon --output evals/results/runtime.json`。

## 从哪里读代码

- [`coding_agent/`](coding_agent/)
- [`long_horizon/`](long_horizon/)
- [`fixtures/`](fixtures/)

## 预期效果

每次试验留下任务、配置、执行结果和独立评分。编码任务可以重复比较，故障注入报告可以定位具体中断场景和证据。

## 设计文档

[评测与经验迭代](../docs/design/evaluation.md)
