# 测试

覆盖模型适配、Agent 工具、Trace、持久化和外部接口的本地回归。

## 怎么使用

安装 `.[dev]` 后执行 `python -m pytest -q`，用模块或用例筛选定位问题。

## 从哪里读代码

- [`test_durable_run_manager.py`](test_durable_run_manager.py)
- [`test_runtime_resume_acceptance.py`](test_runtime_resume_acceptance.py)
- [`test_context_archive.py`](test_context_archive.py)

## 预期效果

用例按功能覆盖命令、工具、工作树和中断恢复。发生失败时可以定位对应行为；依赖特定环境的用例有单独标记。

## 设计文档

[评测与经验迭代](../docs/design/evaluation.md)
