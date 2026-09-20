# 全量回归修复：filesystem trace pollution / MCP crash fixture

日期：2026-09-20

状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 用户本地证据

全量 pytest：

```text
4 failed, 973 passed, 14 skipped, 34 warnings in 168.26s
```

失败：

1. `tests/test_day2.py::TestReflectionNoEdit::test_reflection_triggered_after_no_edit_steps`
2. `tests/test_mcp_integration.py::test_stdio_server_process_crash_maps_to_remote_capability_and_closes`
3. `tests/test_runner.py::test_runner_freezes_acceptance_before_agent_run`
4. `tests/test_runner.py::test_runner_skips_hidden_acceptance_for_incomplete`

## 1. Day2 + Runner：同一根因

`context.repository_state.repository_content_fingerprint()` 在 Git 仓库中走：

```text
git ls-files --cached --others --exclude-standard
```

但测试使用的 `tmp_path` 不是 Git 仓库，因此走：

```text
_filesystem_fingerprint(root)
```

该 fallback 的 skip dirs 原本没有 `logs`。

ExecutionRunner / EventLog 又把 JSONL 写到：

```text
<repo>/logs/
```

于是每一条 Trace append 都改变 filesystem fingerprint。

产生两个错误结果：

### Semantic Progress

```text
Noop Tool
  ↓
EventLog append
  ↓
logs/*.jsonl bytes changed
  ↓
repository_content_changed = true
  ↓
steps_without_semantic_progress = 0
```

所以连续 6 个 no-op 永远触发不了 Day2 `no_edit` reflection。

### Completion Guard

```text
FINISH without code edits
  ↓
EventLog changed
  ↓
final_repo_content_state != initial_repo_content_state
  ↓
require_changes 被误判满足
  ↓
SUCCESS
```

因此 Runner 两个测试预期 INCOMPLETE，实际得到 SUCCESS。

## 2. 修复

`context/repository_state.py` 的 non-Git filesystem fallback 现在忽略顶层：

```text
logs/
```

这与 `agent/loop_detector.py` 已有 `_IGNORED_STATE_DIRS` 中的 `logs` 保持一致。

边界：

- 只影响 **non-Git fallback**。
- Git repository 仍由 `git ls-files` 枚举；如果 `logs/` 是 tracked repository content，它仍会被 fingerprint。
- 不把所有隐藏目录或所有 runtime state 一并忽略，不扩大本轮范围。

新增 `tests/test_repository_state.py`：

1. append `logs/run.jsonl` 不改变 non-Git content fingerprint；
2. 修改真实 `value.txt` 必须改变 fingerprint。

## 3. MCP crash test

失败发生在：

```text
manager.start()
  → client.__aenter__()
  → negotiate_auto()
  → TimeoutError
```

测试本意并不是验证 startup timeout，而是：

```text
server starts
→ crash_process tool discovered
→ call crash_process
→ process exits
→ MCPToolAdapter maps failure to REMOTE_CAPABILITY
→ manager closes
```

正常 stdio fixture 使用 `timeout_seconds=5.0`，crash fixture 单独使用 2.0。在 973+ case 全量 suite 下，2s startup/discovery budget 出现时序性超时。

本轮不修改生产 `MCPClientManager`。只把 crash fixture 的 timeout 调整为 5s，使测试预算与正常 stdio fixture一致；tool 本身仍 `os._exit(7)`，所以实际 crash 语义不变。

## 4. 本地复验顺序

先复跑四个原失败点：

```bash
python -m pytest -q \
  tests/test_day2.py::TestReflectionNoEdit::test_reflection_triggered_after_no_edit_steps \
  tests/test_mcp_integration.py::test_stdio_server_process_crash_maps_to_remote_capability_and_closes \
  tests/test_runner.py::test_runner_freezes_acceptance_before_agent_run \
  tests/test_runner.py::test_runner_skips_hidden_acceptance_for_incomplete \
  tests/test_repository_state.py
```

通过后跑相关模块：

```bash
python -m pytest -q \
  tests/test_day2.py \
  tests/test_runner.py \
  tests/test_mcp_integration.py \
  tests/test_repository_state.py \
  tests/test_agent_skills.py \
  tests/test_structured_planning.py \
  tests/test_structured_recovery.py
```

最后：

```bash
python -m pytest -q
```

当前 ChatGPT 环境无法运行仓库 pytest，因此不记录虚构 passed 数量。
