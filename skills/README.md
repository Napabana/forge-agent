# Skill 发现与加载

发现受信任 Skill，按需加载正文和资源，向模型暴露可用索引。

## 怎么使用

用 `forgeAgent skill list` 检查发现情况，用 `--skills` 在运行中启用。

## 从哪里读代码

- [`catalog.py`](catalog.py)
- [`runtime.py`](runtime.py)

## 预期效果

按需提供仓库约定和操作流程，控制每轮的上下文占用。关联资源跟随 Skill 加载，可按来源和内容摘要核对。

## 设计文档

[Skills 与 MCP](../docs/design/extensions.md)
