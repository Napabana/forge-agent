# P2-4 MCP Client / Tool Adapter 本地回归 DONE

日期：2026-09-19  
收口基线：`dev@f842902e1753bdc69b0217d5aa89033bacd2ae82`  
状态：**DONE**

## 收口结论

P2-4 已完成 official MCP Python SDK v2 client / Forge Tool Adapter 接入，并在用户本地完成修复后 push 到 `dev`。用户随后明确要求结束 P2-4、进入 P2-5，因此按当前项目交接约定将 P2-4 正式收口为 DONE。

核心调用链保持：

```text
LLM
 → ToolRegistry
 → ToolExecutor
 → Hook / Permission / Cancel / Trace
 → MCPToolAdapter
 → MCPClientManager
 → official MCP Python SDK
 → MCP Server
```

MCP 仍只是 external capability provider，没有新增第二套 Agent loop，也没有绕开现有 Tool lifecycle。

## 本地修复事实

当前可核验修复提交：

```text
f842902e1753bdc69b0217d5aa89033bacd2ae82
fix:测试出现的问题
```

该提交包含：

- 修正 `mcp_integration/adapter.py` 的 description 截断边界；
- 为 Chat 测试替身 `FakeSession` 补齐 `close()`；
- 为 GitHub Issue 测试替身 `AssertRunner` 补齐 `close()`；
- 不改变 MCP 生产主链的架构语义。

提交日志明确记录：

```text
定向测试：3 passed
Chat/GitHub Issue 相关测试：25 passed
git diff --check：passed
```

用户本轮没有提供最终全量 pytest 的 passed 数量、完整 stdout 或耗时，因此本日志不补造这些数字。

## Evidence 边界

现在可以说：

- P2-4 MCP Host / Tool Adapter 已实现并完成本地修复收口；
- MCP Tool 继续复用 Forge ToolExecutor / Permission / Hook / cooperative cancel / Planning effect gate / Trace；
- local deterministic MCP fixture、官方 SDK protocol/stdio 测试与 Eval variant 已进入仓库；
- P2-4 状态为 DONE。

仍不能说：

- real-model `planning_recovery_skills vs planning_recovery_skills_mcp` A/B 已执行；
- MCP 提升 coding success rate、pass@1、token efficiency 或 latency；
- local fixture regression 等价于任意第三方 MCP server 的生产可靠性；
- Forge 已实现 MCP marketplace、OAuth 平台、完整 resources/prompts runtime 或 MCP Server。

## 下一阶段

P2 Agent Intelligence 只剩：

```text
P2-5 Trajectory-driven Skill Evolution
```

下一阶段必须保持 eval-gated improvement：trajectory 只能生成 candidate Skill / rule，经 Evaluation Harness 与 promotion gate 后才能进入 approved Skill version；禁止生产运行时自改 Skill 后立即生效。
