# P2-4 MCP Real-model r1：Provider failure 与 Eval isolation 修复

日期：2026-09-21

状态：**R1 INVALID AS MCP OUTCOME / FIX IMPLEMENTED / LOCAL VALIDATION PENDING**

## 1. r1 不是 MCP failure

用户真实执行：

```text
suite: p2-4-mcp-capability-smoke
variant: planning_recovery_skills_mcp
model: deepseek-v4.1-flash
trial_count: 1
```

artifact 已提交到：

```text
evals/results/p2-4-mcp-real-e2e-r1/
```

Trace 首先记录：

```text
mcp_server_capabilities
  server_id=eval_docs
  transport=stdio
  protocol_version=2026-07-28
  tools_supported=true
  resources_supported=true
  prompts_supported=true
  tool_count=5
```

并发现：

```text
mcp__eval_docs__lookup_project_guidance
mcp__eval_docs__echo_text
mcp__eval_docs__record_note
mcp__eval_docs__slow_lookup
mcp__eval_docs__fail_lookup
```

因此 MCP stdio startup / discovery 已进入真实路径。

trial 在 step 4 的模型调用失败：

```text
attempt 1: connection
attempt 2: timeout
final: HTTP 522 / Cloudflare origin connection timeout
```

最终：

```text
run_status=failed
termination_reason=provider_error
acceptance_status=skipped
```

所以本次不能归类为 MCP capability failure。

## 2. r1 暴露 benchmark leakage

r1 的 step 2：

```text
find_files {"pattern":"*.py"}
```

没有在 trial repository 内搜索，而是返回 Forge Agent 源码：

```text
agent/core.py
config/schema.py
context/...
...
```

step 3：

```text
search_text {"pattern":"policy_mode"}
```

进一步返回：

```text
evals/fixtures/coding_agent/mcp_suite.json
...
assert policy_mode() == 'strict'
```

原因：

```text
_build_registry(workspace=trial_repo)
        ↓
file_*      → workspace bound
search_*    → SearchTextTool()/FindFilesTool()/FindSymbolTool()
              no workspace
        ↓
path omitted → Path(".")
        ↓
Forge Agent process cwd
```

这会把 reference fixture 暴露给被测模型。

虽然 MCP suite 的 `mcp-guidance-used` run_trace grader 强制要求：

```text
mcp__eval_docs__lookup_project_guidance
```

所以“猜中 strict”本身不能让 outcome PASS，但 trajectory 已被污染，因此 r1 不能作为干净 real-model evidence。

## 3. 修复

三个 search tools 增加可选 workspace：

```text
SearchTextTool(workspace=...)
FindFilesTool(workspace=...)
FindSymbolTool(workspace=...)
```

production registry 改为：

```text
workspace = target repo/worktree
        ↓
file_*   bound to workspace
search_* bound to workspace
```

规则：

- path 省略：从 target workspace root 搜索；
- 相对 path：相对 target workspace；
- absolute path：必须仍位于 workspace；
- symlink/..` resolve 后逃逸 workspace：拒绝；
- standalone tool 未配置 workspace 时保留历史当前目录行为。

## 4. 显式 config path

本轮之前还出现：

```text
--config config\eval-p2-planning-recovery-v2.yaml
```

在 WSL/Linux 上被当作不存在的字面路径。

旧 `load_config(explicit_path)` 对不存在路径静默：

```text
return _parse({})
```

导致 Eval 表现为：

```text
provider_credentials_not_available
```

而不是告诉用户配置路径错误。

现在显式配置路径不存在时直接抛出 `FileNotFoundError`；若路径含 `\`，错误提示 Linux/WSL 使用 `/`。

只有 `load_config(None)` 找不到默认配置时仍允许返回默认配置，保持无显式 config 的兼容语义。

## 5. 验证

先跑：

```bash
python -m pytest -q \
  tests/test_cli_isolate.py \
  tests/test_day3.py \
  tests/test_day6.py \
  tests/test_mcp_integration.py \
  tests/test_coding_agent_eval.py
```

通过后跑：

```bash
python -m pytest -q
```

然后用新目录执行：

```bash
python -m evals.coding_agent \
  --config config/eval-p2-planning-recovery-v2.yaml \
  --suite evals/fixtures/coding_agent/mcp_suite.json \
  --variant planning_recovery_skills_mcp \
  --repetitions 1 \
  --real-model \
  --output-dir evals/results/p2-4-mcp-real-e2e-r2
```

r2 必须检查：

1. `find_files/search_text/find_symbol` 不再返回 Forge Agent source / eval fixture；
2. Trace 有 5 个 MCP tool discovery；
3. 模型实际调用 `mcp__eval_docs__lookup_project_guidance`；
4. MCP result 给出 canonical file + required policy；
5. repository 被修改为 `POLICY_MODE = 'strict'`；
6. behavior / canonical-setting / mcp-guidance-used 三个 grader 均 PASS；
7. 若再次 Provider 522，单独记 provider_error，不计为 MCP capability failure。
