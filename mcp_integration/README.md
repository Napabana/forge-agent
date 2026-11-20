# MCP 集成

连接 MCP server，将发现的工具转换为 Forge 工具。

## 怎么使用

用 `forgeAgent mcp test ID` 验证配置和工具发现。

## 从哪里读代码

- [`manager.py`](manager.py)
- [`adapter.py`](adapter.py)
- [`registry.py`](registry.py)

## 预期效果

外部服务以带命名空间的工具进入 Forge 执行链。MCP 模块负责连接、发现和调用，并把返回结果转成统一的工具观察。

## 设计文档

[Skills 与 MCP](../docs/design/extensions.md)
