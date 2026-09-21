# P2-4 MCP：pr-test Real-model E2E DONE

日期：2026-09-21

状态：**DONE**

Forge Agent：

```text
dev@a38fe865de879c8041df076eeb333ba30cbcaa61
fix: normalize MCP guidance topics
```

Target repository：

```text
Napabana/pr-test
branch: forge-p2-mcp-demo
baseline: 5f10fa18f48068ba9fa4c0bb30c0f76332c104f8
```

模型：

```text
deepseek-v4.1-flash
OpenAI-compatible / chat_completions
```

## 1. 最终结果

真实模型运行最终：

```text
Status  : SUCCESS
Steps   : 29
Tokens  : 295,016
Time    : 779.2s
Acceptance: not_requested
```

最终业务状态：

```text
src/app/config.py:
POLICY_MODE = "strict"

behavior:
policy_mode -> strict

tests:
16 passed in 0.61s
```

未创建 Git commit。

## 2. MCP 主链证据

修复 natural topic routing 后，第一次 MCP 调用：

```text
Step 1
tool=mcp__eval_docs__lookup_project_guidance
topic="policy mode configuration"

guidance:
Set POLICY_MODE = 'strict' in src/app/config.py;
that file is the canonical organization policy setting.
```

这说明真实模型不再需要猜 fixture 的精确 key `policy`。

后续真实链：

```text
MCP discovery
    ↓
model selects mcp__eval_docs__lookup_project_guidance
    ↓
ToolExecutor
    ↓
MCPToolAdapter
    ↓
official MCP client / local stdio server
    ↓
structured external guidance
    ↓
read pr-test canonical config/runtime/tests
    ↓
plan_create
    ↓
file_write src/app/config.py
    ↓
post-edit full pytest: 16 passed
    ↓
behavior check: strict
    ↓
plan step completion
    ↓
FINISH
    ↓
SUCCESS / completion_satisfied
```

因此 P2-4 的关键 claim 已获得真实模型证据：

1. namespaced MCP tool 可被模型发现并选择；
2. MCP invocation 继续走 Forge Tool lifecycle；
3. MCP structured result 被模型实际用于 coding decision；
4. 外部 capability 决定 canonical path + required value；
5. repository edit 发生在独立 pr-test；
6. 修改后重新验证；
7. Completion Guard 允许 FINISH。

## 3. Completion / Acceptance 边界

终端打印：

```text
Acceptance: not_requested
```

这不是“没有完成性校验”。

Runner 的 `acceptance_status` 表示额外 independent `AcceptanceContract` 是否被请求。

本轮 CLI Task 描述包含 change/test intent，Agent Core 内建 Completion Guard 仍检查：

```text
require_changes
require_tests
latest test passed
final repository content state == last successful test content state
```

本轮在最终修改后执行 `tests/` 并通过，之后没有再次修改 repository content，最终 `SUCCESS` / `completion_satisfied`。

所以允许写：

> Agent 在使用 MCP guidance 修改目标仓库后，完成 post-edit full test，并通过内建 Completion Guard 收口。

不能把 `Acceptance: not_requested` 写成独立 hidden acceptance 已通过。

## 4. 本轮仍然很低效

尽管功能通过，本轮 trajectory 明显存在与 MCP 无关的浪费：

- Step 5/8/9 检查 CRLF 与 test 文件字节；
- Step 10 在修改前先跑了一次 focused test；
- Step 12/15/18/21 多次重复写同一 config；
- Step 17 shell redirection 被 Permission 拒绝；
- Step 19 因 no-progress 触发 plan revision；
- Step 20/22 继续处理 trailing newline；
- 中途一次模型 timeout；
- Step 13 的 git diff 还受 clone 中预先存在的 line-ending 工作树噪声影响。

因此：

```text
29 steps
295,016 tokens
779.2s
```

只记录为这次成功 run 的事实，不用于声称 MCP 提升或降低效率。

如果后续做正式 benchmark，应先冻结：

```text
git config core.autocrlf false
git reset --hard
git clean -fdx
```

并确保 trial 开始时 `git status --short` 为空。

## 5. 与前一轮 INCOMPLETE 对照

上一轮：

```text
30 steps
343,717 tokens
339.8s
INCOMPLETE / max_steps
```

根因是 MCP fixture exact-key trap：多个自然语言 policy topic 被 fallback 到 navigation。

本轮：

```text
Step 1 首次 MCP query 即获得 policy guidance
最终 SUCCESS
```

这证明 natural topic normalization 修复了 capability fixture 的语义可用性问题。

但由于本轮有 provider timeout 与 newline/CRLF 探索，不能把 step/token/time 差异解释为稳定性能改善。

## 6. 已验证与未验证

```text
MCP stdio startup/discovery                 VERIFIED
Namespaced tool discovery                   VERIFIED
Real model MCP tool selection               VERIFIED
ToolExecutor → MCP adapter invocation       VERIFIED
Structured MCP result consumption           VERIFIED
pr-test repository edit                     VERIFIED
Post-edit full tests                        VERIFIED
Completion Guard SUCCESS                    VERIFIED

MCP performance improvement                 NOT CLAIMED
Repeated-run pass@1                         NOT MEASURED
Real-model shell Docker isolation           NOT VERIFIED IN THIS RUN
External production MCP server reliability  NOT CLAIMED
```

## 7. P2-4 结论

P2-4 MCP Client / Tool Adapter 至此同时具备：

```text
deterministic regression
+
official SDK local stdio protocol coverage
+
real-model pr-test end-to-end capability evidence
```

P2-4 可以正式收口为 **DONE**。

下一阶段进入 P2-5 Trajectory-driven Skill Evolution 的真实流程验收。
