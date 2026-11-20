# 编码任务评测

为同一任务准备可比较的工作区和配置，用生产 Runner 执行并通过独立 grader 评分。评测报告连接任务、运行、Trace 与评分结果。

```bash
python -m evals.coding_agent --help
```

`schema.py` 定义任务和结果，`runner.py` 驱动试验，`graders.py` 提供独立评分，`report.py` 汇总。显式启用真实模型前可先验证任务与配置。[评测设计](../../docs/design/evaluation.md)
