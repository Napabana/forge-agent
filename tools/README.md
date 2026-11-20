# 内置工具

提供读写、查找、符号导航、测试、Shell、Git、运行容器及归档读取能力。

## 怎么使用

让 Agent 在运行任务时调用。直接集成时按工具 schema 注册和传参。

## 从哪里读代码

- [`base.py`](base.py)
- [`file_tool.py`](file_tool.py)
- [`git_tool.py`](git_tool.py)
- [`runtime.py`](runtime.py)
- [`archive_read.py`](archive_read.py)

## 预期效果

模型用一致的参数和结果格式操作工作区。各工具实现负责具体行为，执行记录可追溯工具返回的数据。

## 设计文档

[Agent 与工具执行](../docs/design/agent-tools.md)
