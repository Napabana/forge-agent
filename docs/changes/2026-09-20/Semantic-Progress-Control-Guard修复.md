# Semantic Progress control-action guard 修复

日期：2026-09-20

状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 现象

本地专项回归：

```text
FAILED tests/test_agent_skills.py::test_skill_load_is_semantic_progress_but_duplicate_load_is_not
assert 0 == 1
```

同一轮其余专项为 156 passed，另有 2 个 AnyIO/Python 3.11 cancellation deprecation warnings；这些 warning 与本失败无直接关系。

## 根因

`agent/core.py` 的 Skill/Planning control branch 已经正确维护：

```text
steps_without_semantic_progress
```

例如重复 `skill_load`：

```text
already_loaded = true
semantic progress = false
counter += 1
```

但随后 control branch 直接：

```text
continue
```

因此只要模型持续调用重复 Skill control 或 Planning no-op，计数虽然增加，却不会执行后面的 NO_PROGRESS threshold。

原测试还在 duplicate Skill load 后执行第三次完全相同的 `file_read`。该 read 会进入 LoopDetector；三次完全相同 read 满足 period=1/repeats=3 时，Loop recovery 会先于 NO_PROGRESS guard 处理并 `continue`，因此最终看不到预期 NO_PROGRESS event。

这说明问题不只是测试隔离：control action 确实存在绕过 progress guard 的真实路径。

## 修复

新增：

```text
Agent._apply_no_progress_guard(...)
```

统一处理：

- threshold 判断
- tested repository change 的既有 exemption
- structured NO_PROGRESS recovery
- legacy reflection
- recovery exhausted terminal result
- counter reset

现在三类路径共用同一 guard：

```text
ordinary Tool
Skill control
Planning control
```

因此：

- first `skill_load` → semantic progress → counter reset
- duplicate `skill_load` → no progress → counter increment → threshold check
- idempotent Planning no-op → no progress → counter increment → threshold check
- ordinary read → no progress → counter increment → threshold check

## 测试调整

`tests/test_agent_skills.py::test_skill_load_is_semantic_progress_but_duplicate_load_is_not` 不再依赖第三次相同 read 才触发 NO_PROGRESS。

新脚本：

```text
file_read
skill_load(first)      -> progress reset
file_read              -> no-progress count 1
skill_load(duplicate)  -> no-progress count 2 -> NO_PROGRESS
FINISH
```

并断言：

1. 两次 `SKILL_LOADED`；
2. 第一次 `already_loaded=false`；
3. 第二次 `already_loaded=true`；
4. 恰好一次 `failure_classified(category=no_progress)`；
5. NO_PROGRESS event 位于 duplicate Skill load 之后；
6. NO_PROGRESS 与 duplicate Skill load 处于同一 step。

## 验证要求

请先重跑失败测试：

```bash
python -m pytest -q \
  tests/test_agent_skills.py::test_skill_load_is_semantic_progress_but_duplicate_load_is_not
```

再跑本轮专项：

```bash
python -m pytest -q \
  tests/test_structured_planning.py \
  tests/test_structured_recovery.py \
  tests/test_agent_skills.py \
  tests/test_agent_completion_guards.py \
  tests/test_cli_isolate.py \
  tests/test_tool_schema_strictness.py \
  tests/test_openai_responses.py \
  tests/test_openai_terminal_actions.py \
  tests/test_model_aware_token_budget.py \
  tests/test_api.py \
  tests/test_chat.py \
  tests/test_github_issue_delivery.py
```

最后：

```bash
python -m pytest -q
```

当前 ChatGPT 环境未真实执行 pytest，因此状态保持 `LOCAL VALIDATION PENDING`。
