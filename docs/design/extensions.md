# Skills 与 MCP

## 为什么实现

Skills 提供按需载入的工作方法，MCP 适配外部工具，使扩展能进入统一调用链。

## 怎么使用

用 `forgeAgent skill add PATH` 安装 Skill，随后通过 `skill list`、`inspect`、`update`、`remove` 管理。用 `forgeAgent mcp add ID --url https://example.com/mcp` 注册服务，并用 `mcp test ID` 检查发现结果。

## 怎样实现

Skill Catalog 扫描作用域，Skill Runtime 加载正文及受限引用。安装记录来源和内容摘要，按根目录串行执行并恢复中断的目录更新。MCP Client Manager 管 stdio 与 Streamable HTTP 的连接、发现和超时。适配器把服务器工具注册为带命名空间的 Forge 工具。

## 预期效果

扩展可以独立维护，工具 schema、事件和结果仍用现有执行机制观察。

## 使用边界

项目 Skill 受 Workspace Trust 控制。MCP 只读注解的采信需显式配置。效果未知时只查询同一服务上已配置的只读回执工具。
