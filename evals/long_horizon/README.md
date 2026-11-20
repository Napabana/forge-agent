# 长时运行可靠性评测

独立试验将真实生产代码置于固定中断点，验证检查点恢复、操作日志、归档、Owner 和用量口径。

```bash
python -m evals.long_horizon --output evals/results/runtime-reliability.json
```

`__main__.py` 声明场景并汇总结果，`pytest_plugin.py` 采集逐 Trial 证据。报告保存判定、失败点、关联操作和 Trace。[运行时设计](../../docs/design/durable-runtime.md)

## 结果边界

指标按有观测证据的场景计量，主要验证系统行为与恢复契约。
