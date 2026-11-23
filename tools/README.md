# 内置工具

提供读写、查找、符号导航、测试、Shell、Git、运行容器及归档读取能力。

## 怎么使用

让 Agent 在运行任务时调用。直接集成时按工具 schema 注册和传参。

`test` 的调用状态与 `test_outcome` 分开。调用方读取 `passed`、`failed` 或 `no_tests` 判断 pytest 的结论，参数、进程及环境故障按工具错误类型处理。

`shell` 从 Runtime 读取进程状态和退出码。持久化用这份证据区分正常退出、超时和回执丢失。

## 从哪里读代码

- [`base.py`](base.py)
- [`file_tool.py`](file_tool.py)
- [`test_tool.py`](test_tool.py)
- [`shell_tool.py`](shell_tool.py)
- [`git_tool.py`](git_tool.py)
- [`runtime.py`](runtime.py)
- [`archive_read.py`](archive_read.py)

## 预期效果

模型用一致的参数和结果格式操作工作区。各工具实现负责具体行为，执行记录可追溯工具返回的数据。

## 设计文档

[Agent 与工具执行](../docs/design/agent-tools.md)
