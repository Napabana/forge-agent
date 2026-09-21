# P2-4 MCP pr-test E2E：Guidance Topic Normalization

日期：2026-09-21

状态：**FIX IMPLEMENTED / LOCAL VALIDATION PENDING**

## 现象

真实 E2E 使用：

- Forge Agent：`dev`
- Target repo：`Napabana/pr-test:forge-p2-mcp-demo`
- MCP server：`eval_docs`
- Model：`deepseek-v4.1-flash`

MCP tool 已真实执行成功，但前五次自然语言 topic：

```text
policy_mode
policy mode configuration
organization policy
config
required policy mode value
```

没有返回 required policy value。

直到第六次：

```text
topic = policy
```

才返回：

```text
Set POLICY_MODE = 'strict' in src/app/config.py
```

随后 Agent：

- 写入 `src/app/config.py`
- runtime 验证输出 `'strict'`

但已经到 Step 30，最终：

```text
Status: INCOMPLETE
termination: max_steps/resource_exhausted
Steps: 30
Tokens: 343,717
Time: 339.8s
```

## 根因

旧 fixture：

```python
guidance.get(normalized, guidance["navigation"])
```

等价于要求模型猜中固定 enum-like key：

```text
policy
```

这不是 MCP capability 本身需要验证的能力。

## 修复

先 normalize：

```text
policy_mode
policy-mode
policy   mode
→ policy mode
```

再按语义关键词路由：

```text
contains policy              → policy
contains config/configuration→ configuration
contains test/verify         → tests
otherwise                    → navigation
```

因此真实任务描述中自然产生的：

```text
required policy mode value
organization policy
policy_mode
```

都会得到同一 deterministic policy guidance。

## 证据边界

前一轮虽然最终 INCOMPLETE，但已经真实证明：

```text
MCP discovery
→ model selects MCP tool
→ ToolExecutor
→ MCPToolAdapter
→ MCP server call
→ structured guidance result
→ repository edit
```

主链可工作。

它没有证明完整 task success，因为重复无效 guidance 消耗了 step budget。

## 验证

先运行：

```bash
python -m pytest -q \
  tests/test_mcp_integration.py \
  tests/test_cli_isolate.py \
  tests/test_day3.py \
  tests/test_day6.py
```

通过后建议再跑全量：

```bash
python -m pytest -q
```

然后重置或重新 clone：

```text
Napabana/pr-test
branch: forge-p2-mcp-demo
```

重新执行 `agent --config config/eval-p2-mcp-pr-test.yaml run ...`。

新的真实 E2E 重点检查：

1. 第一次合理的 policy-like MCP query 即返回 strict guidance；
2. 不再需要连续重复 MCP lookup 才猜中 exact key；
3. repository edit 仍只发生在 pr-test；
4. tests 通过；
5. Agent 在 max_steps 前 FINISH。
