# P2 Stage 1 真实运行收口：Planning / Skills / Shell Effect

日期：2026-09-20  
问题来源：真实模型运行 `599cb93d_20260920_081010`  
实现提交：`94810f962a81457d52793a9442406460664d849f`  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 1. 真实运行事实

第三轮 Stage 1 真实运行最终：

```text
Status: SUCCESS
Steps: 19
termination_reason: completion_satisfied
```

该轮已经真实验证：

- Structured Planning 创建并维护 Plan v1；
- pytest failure 被分类为 `test_failure`；
- Structured Recovery 选择 `inspect`；
- 最小代码修复后 focused test 与 full pytest 均通过；
- Completion Guard 最终允许 FINISH；
- 前一轮 `git commit -> FINAL_STATE_UNVERIFIED` 的误判链未再出现。

但 Trace 同时暴露：

1. 只读 shell（`cat ...; echo ...; cat ...`）在 Plan 创建前被错误视为 repository mutation；
2. 两次 `skill_load` 都缺少有效 `name`，形成 `skill_rejected`；
3. 无效 `skill_load` 仍被记录成空的 `skill_selected`；
4. `plan_step_update` 缺少 step_id 或重复更新 terminal step 时，恢复提示不够明确。

因此本轮 SUCCESS 不等于 Skills 完整链已经验收。

## 2. Shell invocation-level effect

此前 `ToolRegistry.is_mutating()` 只读取静态 `tool.effect`。

`ShellTool` 没有覆盖 effect，因此继承：

```text
MAY_MUTATE_REPOSITORY
```

导致所有 shell，包括 `cat` / `pytest` / `git status`，都会被 Planning Gate 保守拦截。

本轮新增：

```python
BaseTool.is_mutating(params)
ToolRegistry.is_mutating(name, params)
ShellTool.is_mutating(params)
```

默认工具仍按静态 effect fail-safe；Shell 根据具体 command 做 invocation-level classification。

只读示例：

```text
cat pytest.ini; echo ---; cat calculator.py
pytest -q
git status --short
```

mutation-capable 示例：

```text
echo fixed > value.txt
git commit -m fix
cat value.txt; rm value.txt
```

该分类仅用于 Structured Planning gate，不改变 PermissionManager 的安全判断。

## 3. Skill control contract

真实 Trace 中两次出现：

```text
skill_load({})
→ SKILL_SELECTED skill=""
→ SKILL_REJECTED unknown skill
```

本轮修复：

- `skill_load` 失败时返回可用 catalog name 的明确恢复提示；
- 只有非空且确实存在于 catalog 的 skill name 才记录 `SKILL_SELECTED`；
- 缺失/未知 name 只记录 `SKILL_REJECTED`，避免 Trace 产生假的 selection evidence。

完整 Skill 成功链仍需下一轮真实模型运行验证：

```text
skill_discovered
→ skill_selected verify-python-fix
→ skill_loaded verify-python-fix
```

## 4. plan_step_update contract

Runtime context 现在明确：

- `plan_step_update` 必须使用现有 step_id；
- 只能更新 `pending` / `in_progress` step；
- 不要重复更新 `completed` / `skipped` step。

被拒绝时会返回当前可更新的 non-terminal step ids，减少兼容模型重复猜参数。

## 5. 回归覆盖

新增/扩展测试覆盖：

- read-only compound shell 在 Planning 前允许；
- mutation shell 仍被判为可写；
- unknown tool 继续 fail-safe；
- terminal plan step 重复更新返回 non-terminal step 提示；
- 缺失 Skill name 不产生假的 `SKILL_SELECTED`；
- Skill rejection 提示包含可用 catalog 名称。

本日志创建时尚未收到用户本地 pytest 验证结果，因此不补造 passed 数量或耗时。

## 6. Evidence boundary

当前可以证明实现层已完成上述契约收口。

尚不能声称：

- 真实模型一定会成功加载 Skill；
- Stage 1 Skills 已完整 E2E 验收；
- Planning / Skills 降低平均 token、step 或 latency；
- Shell 动态分类覆盖所有 shell grammar。

复杂 shell expression 仍采取保守策略，无法可靠证明 read-only 时按 mutation-capable 处理。
