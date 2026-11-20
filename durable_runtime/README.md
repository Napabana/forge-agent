# 持久化运行时

管理 SQLite、Scope Owner、检查点、工具日志和未知副作用核对。

## 怎么使用

配置启用持久化后，通过 `runtime` 命令查看与恢复。

## 从哪里读代码

- [`store.py`](store.py)
- [`run_manager.py`](run_manager.py)
- [`operation_journal.py`](operation_journal.py)
- [`recovery.py`](recovery.py)
- [`reconcilers.py`](reconcilers.py)

## 预期效果

任务中断后能定位最后一个已提交的检查点并复用已保存的工具结果。效果尚待核实的操作有日志和恢复入口。接管、归档与计量的完整流程见设计页。

## 设计文档

[长时运行与恢复](../docs/design/durable-runtime.md)
