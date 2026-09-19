# P2-4 MCP Client / Tool Adapter

日期：2026-09-19  
实现基线：`dev@1a3e72318ba2df1722a0dede0ec55ca916282abd`  
状态：**DONE**

## 目标与最终主链

本轮把 Forge 作为 MCP Host，但 MCP 只提供 external capability，不建立第二套 Agent/tool loop：

```text
LLM
 ↓
normal ToolCall
 ↓
ToolRegistry
 ↓
ToolExecutor
 ├─ validation
 ├─ pre-hook
 ├─ permission
 ├─ cooperative cancel
 └─ Trace v2
 ↓
MCPToolAdapter
 ↓
MCPClientManager
 ↓
official MCP Python SDK
 ↓
MCP Server
```

Agent 没有任何 `Agent → MCP Client` 直连路径。

## 官方 SDK 核对

实现按 2026-09-19 的官方 Python SDK v2 stable API 收口：

- dependency：`mcp>=2.2.0,<3`；
- `Client` 是 async context manager；
- stdio 使用 `StdioServerParameters`；
- URL target 使用 Streamable HTTP；
- `Client(MCPServer)` 可做 in-process protocol test；
- Python v2 model field 使用 snake_case；
- ToolAnnotations 只是 hint，不作为默认安全证明。

不实现自己的 JSON-RPC parser，不新增顶层 `mcp/` package，不使用旧 SSE 作为新 transport 主线。

## 设计审查结论

1. **async lifecycle owner**：`MCPClientManager` 唯一持有 connection；官方 Client 的 enter/exit 都发生在同一个 supervisor task。
2. **sync/async bridge**：manager 拥有 dedicated asyncio loop thread；同步 adapter 用 `run_coroutine_threadsafe` 调长期连接，不为每次 ToolCall `asyncio.run()`。
3. **registry / cleanup**：discover 后 adapter 注册进现有 registry；direct Runner close manager，isolate run 的 manager 在 worktree/runtime finalize 前 close。
4. **isolate**：每个 isolate/worktree run 都创建独立 MCP lifecycle，不跨 worktree 复用 connection。
5. **Chat**：同一 ChatSession 复用其 ExecutionRunner/manager；ChatSession.close 再 deterministic cleanup。
6. **name namespace**：`mcp__<server_id>__<remote_tool>`，deterministic sanitize + bounded name；adapter 保留原始 server/tool；collision 显式失败。
7. **schema / metadata bounds**：仅接收 JSON-serializable object schema，malformed/non-object 直接拒绝；单 schema 限 32k 字符、单 description 限 2k 字符、单 server 最多 128 tools，避免不可信 capability metadata 无界占用 Host/context。
8. **annotations/effect**：server 默认不信任 remote read-only hint。仅 `trust_read_only_annotations=true` 且 `read_only_hint=true` → READ_ONLY；其余 MAY_MUTATE_REPOSITORY。
9. **permission**：READ_ONLY MCP 可 ALLOW；其余 MCP 默认 CONFIRM。决策只在 ToolExecutor/PermissionManager 发生。
10. **errors**：application `is_error` → recoverable tool failure；recognizable input validation → INVALID_ARGUMENTS；timeout → TIMEOUT；stdio startup/discovery、disconnect/protocol/server failure → REMOTE_CAPABILITY；manager lifecycle invariant → INFRASTRUCTURE。
11. **Trace**：继续写现有 tool_execution_* / permission / observation；增加 `mcp_server_capabilities` 与 `mcp_tool_discovered`。前者记录 protocol/server name+version/tools-resources-prompts/tool_count，后者和 Tool span 记录 server/tool/transport/safety hint；都不记录 URL/env/secret，不建 Trace v3。
12. **secret**：Trace metadata 不包含 server env/url/header/key；stdio 只把 config 显式 env 交给 SDK，SDK 自身仅补最小默认环境，不转发完整 `os.environ`。
13. **server crash**：已开始的 remote call 失败转为 REMOTE_CAPABILITY Observation，可被 P2-2 Recovery 消费，不把 Forge framework 标为崩坏；不做无界 reconnect/retry。
14. **HTTP**：官方 v2 Client 对 URL 使用同一 lifecycle abstraction，因此首版一并支持薄 Streamable HTTP 分支；OAuth/headers 平台化仍延期。

## Config

```yaml
mcp:
  enabled: false
  servers:
    - id: local_docs
      enabled: true
      transport: stdio
      command: python
      args: [path/to/server.py]
      env: {}
      timeout_seconds: 30
      trust_read_only_annotations: false
```

server id 有稳定 regex/duplicate 校验。默认 MCP disabled。现有 config 的 `${ENV_VAR}` expansion 机制继续生效。

## Evaluation Harness

新增 architecture variant：

```text
planning_recovery_skills_mcp
  planning=always
  recovery=structured
  skills=on
  mcp=fixed local stdio fixture
```

fixture：`evals/fixtures/coding_agent/mcp/server.py`。它只提供 deterministic docs/echo/timeout/error/permission-classification capability，不重新包装 file/shell/test。

另新增独立 `evals/fixtures/coding_agent/mcp_suite.json`，不修改 P2-0 已冻结的 8-case suite。该 MCP-specific case 的任务内容本身不直接给出目标 policy 值，process grader 明确要求 Trace 中调用 `mcp__eval_docs__lookup_project_guidance`，outcome 仍由 repository-local deterministic grader 判断。

新增 metrics：

- `mcp_tool_discovered_count`
- `mcp_tool_call_count`
- `mcp_tool_failure_count`

不覆盖历史 frozen `evals/results`。

## Deterministic regression added

`tests/test_mcp_integration.py` 覆盖：

- namespace/collision；
- malformed / oversized schema、empty name、description/tool-count bounds；
- read-only trust boundary / ToolEffect；
- text + structured bounded rendering；
- isError / invalid args / timeout / remote failure；
- ALLOW / CONFIRM accept/reject；
- hooks；
- cancel before tool / cancel during synchronous bridge；
- official SDK in-process discover/list/call；
- official stdio discover/tools-resources-prompts capability/list/invoke/error/timeout/close；
- stdio startup failure → REMOTE_CAPABILITY；
- Streamable HTTP official Client URL transport construction；
- stdio server process crash；
- multiple server isolation；
- direct Runner 跨 run 复用同一 manager，close 后连接终止；
- isolate/worktree 每次 run 独立 MCP lifecycle；
- ExecutionRunner → Agent → ToolExecutor → MCP adapter → stdio server E2E；
- Planning mutation gate；
- structured Recovery 可见 REMOTE_CAPABILITY；
- fixed Eval variant mapping、独立 MCP-specific suite/reference self-check；
- config / package discovery。

测试代码已加入，且用户随后在本地完成修复并 push。当前可核验修复提交 `f842902e1753bdc69b0217d5aa89033bacd2ae82` 的日志记录：定向测试 3 passed、Chat/GitHub Issue 相关测试 25 passed、`git diff --check` 通过。用户未提供最终全量 pytest 的 passed 数量、完整 stdout 或耗时，因此不补造数字。

## 明确未做

- P2-5 Trajectory-driven Skill Evolution；
- Multi-Agent；
- MCP marketplace / remote install；
- Forge 自己作为 MCP Server；
- full resources subsystem / full prompts subsystem；
- sampling bridge / elicitation UI；
- OAuth/Authorization 平台化；
- old SSE 主线；
- unbounded retry/reconnect；
- native file/shell/test 的 MCP 重写；
- dangerous external real account E2E；
- real-model paid A/B；
- 历史 frozen result 修改。

resources/prompts 目前只通过 server capabilities snapshot 做 telemetry，不注入 Agent context。

## Evidence 边界

当前可以说：

- MCP Host implementation 已接入现有 Tool lifecycle；
- official SDK local fixture / deterministic tests 已存在；
- P2-0 MCP architecture variant 已存在。

当前不能说：

- deterministic regression passed；
- real MCP stdio E2E passed；
- MCP 提升 coding success/pass@1/token/latency。

上述通过性结论必须等用户本地真实执行后再补。

## 本地验证

详见本轮最终回复 A-H 命令。用户本地验证通过前，本文件状态保持：

```text
IMPLEMENTED / LOCAL VALIDATION PENDING
```
