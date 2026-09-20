# P2 Stage 1 真实运行收口：Planning / Skills / Shell Effect

日期：2026-09-20  
问题来源：真实模型运行 `599cb93d_20260920_081010`  
实现提交：`94810f962a81457d52793a9442406460664d849f`  
状态：**DONE（LOCAL REGRESSION + REAL-MODEL E2E）**

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

用户已完成本轮建议的本地专项 pytest 回归，并确认测试通过。未提供完整 pytest stdout、passed 数量与耗时，因此不补造具体统计数字。

## 6. Evidence boundary

当前可以证明实现层已完成上述契约收口。

尚不能声称：

- 真实模型一定会成功加载 Skill；
- Stage 1 Skills 已完整 E2E 验收；
- Planning / Skills 降低平均 token、step 或 latency；
- Shell 动态分类覆盖所有 shell grammar。

复杂 shell expression 仍采取保守策略，无法可靠证明 read-only 时按 mutation-capable 处理。


---

## 7. Stage 1 最终真实模型 E2E

最终验收运行：

```text
run_id: ff98262c_20260920_121748
model: deepseek-v4.1-flash
provider protocol: OpenAI-compatible
status: SUCCESS
steps: 18
tokens: 122,597
wall time: 98.6s
termination_reason: completion_satisfied
acceptance: not_requested
```

测试仓库使用 `Napabana/pr-test` 的独立 `forge-p2-skill-demo` fixture。
任务要求修复 Python regression，并按照仓库特有 release verification contract 完成 focused / full / repository-specific verification。

最终真实执行链：

```text
skill_discovered verify-release-contract
        ↓
generic file_read 尝试读取 .agents/skills/.../SKILL.md
        ↓
runtime 拒绝 generic Skill bypass
        ↓
failure_classified: tool_failure
        ↓
recovery_selected: inspect
        ↓
skill_selected verify-release-contract
        ↓
skill_loaded verify-release-contract
        ↓
repository-specific verifier instructions 进入 runtime context
        ↓
pytest failure
        ↓
failure_classified: test_failure
        ↓
recovery_selected: inspect
        ↓
plan_created v1
        ↓
plan_step_completed: inspect
        ↓
file_write 修复 normalize_label
        ↓
focused pytest: 4 passed
        ↓
full pytest: 19 passed
        ↓
python scripts/verify_release_contract.py
        ↓
release contract: OK
        ↓
plan_step_completed: edit / verify
        ↓
FINISH
        ↓
run_terminated: completion_satisfied
```

关键 Trace 事实：

- `skill_discovered`：project Skill `verify-release-contract` 被发现，但完整正文未在 startup metadata 中常驻；
- generic `file_read` 访问 `.agents/skills/**` 被拒绝，证明 progressive disclosure 不能再被普通 repository tool 绕过；
- 随后真实产生 `skill_selected` 与 `skill_loaded`；
- test failure 被分类为 `test_failure`，Structured Recovery 选择 `inspect`；
- `plan_created` 后三个 step 均进入 `plan_step_completed`；
- 最终没有 `completion_rejected`，运行以 `completion_satisfied` SUCCESS 收口。

因此 Stage 1 已获得真实模型下的 Planning + Skills + Recovery + Completion 联合证据，不再仅是 deterministic unit/regression evidence。

## 8. Stage 1 收口过程中修复的 runtime 边界

最终 E2E 前的真实运行还暴露并修复了以下问题：

### 8.1 Direct-run Git ownership

普通 `agent run` 不再向模型暴露 `git_add/git_commit`，mutating Git 也不能通过 shell 绕过。
Direct run 只负责 working-tree 修改；commit/push/PR 属于显式 delivery runtime。

### 8.2 Completion Guard 使用 repository content semantics

Git HEAD / staging metadata 变化不再误触发 `FINAL_STATE_UNVERIFIED`。
测试后的真实文件内容如果没有变化，`git add/commit` 本身不会要求冗余 retest；
如果文件字节真实变化，仍然必须重新验证。

### 8.3 Shell invocation-level effect

Planning Gate 不再把所有 shell 静态视为 MAY_MUTATE。
简单只读 `;` / `&&` chain（例如 `cd repo && cat ...`）可在 plan 前用于探索；
复杂 shell grammar / redirection / mutation 仍 fail-safe。

### 8.4 Skill runtime-owned path

`.agents/skills/**` 的完整正文不能通过：

- file_read / file_view / file_write
- search_text / find_files
- Repo Map
- shell 直接读取

绕过。
完整 Skill 必须通过 `SkillRuntime` 的 `skill_load` / `skill_reference_load` progressive disclosure contract 进入 context。

### 8.5 Recovery replan 不再压过 Completion Guard

pending replan 仍可阻止后续 repository mutation，但不再直接阻止 FINISH。
FINISH 是否成立最终由 repository/test/acceptance Completion Guard 判定，避免“代码和测试已完成但 plan revision 格式错误”导致控制面死锁。

### 8.6 已验证修改后的 verification 不再误判 NO_PROGRESS

当当前 repository content 已经发生修改，且与最近一次成功 test 对应的 content fingerprint 一致时，
后续 verifier / diff / read-only inspection 不再仅因为“没有继续编辑”触发 `NO_PROGRESS → REPLAN`。

## 9. Stage 1 最终状态

```text
P2 Stage 1
├── Structured Planning             DONE / real-model E2E
├── Failure Classification          DONE / real-model E2E
├── Structured Recovery             DONE / real-model E2E
├── Skill Discovery                 DONE / real-model E2E
├── Skill Progressive Disclosure    DONE / real-model E2E
├── Skill Runtime Isolation         DONE / real-model E2E
├── Repository-specific Skill       DONE / real-model E2E
├── Completion after Recovery       DONE / real-model E2E
└── Direct-run Git ownership        DONE / regression + E2E observation
```

**Stage 1 = DONE。**

## 10. Stage 1 留下但不再阻塞 DONE 的已知债务

最终 E2E 同时留下两组明确的下一阶段基础设施问题。这些问题不推翻 Stage 1 功能闭环，但应在继续扩展 Agent 能力前收口。

### Planning v2

1. **runtime-owned step identity**
   - 当前模型仍生成 plan step `id`，会产生缺失、非法、重复或不稳定 identity；
   - step identity 属于 runtime bookkeeping，应由 PlanningRuntime 确定性生成，并保留可读 Trace identity。

2. **idempotent plan_step_update**
   - 重复确认同一 terminal state 不应制造无价值 `PLAN_REJECTED`；
   - 真正状态回退（如 completed → in_progress/pending）仍应拒绝。

3. **provider-aware strict schema**
   - 当前 OpenAI-compatible adapter 只发送 JSON Schema parameters，没有 provider capability-aware strict enforcement；
   - 兼容模型真实出现过缺少 `step_id`、`reason`、`goal`、`steps` 等 malformed control calls；
   - strict support 必须 capability-aware，不能假设所有 OpenAI-compatible gateway 都等价支持。

4. **减少 plan state 由模型维护**
   - runtime 已经知道 current plan/version/step status 时，不应继续要求模型重复维护纯 bookkeeping；
   - 保持语义 planning 由模型产生，状态 identity/version/合法 transition 尽量 runtime-owned。

### Recovery v2 / Progress semantics

1. **Recovery budget semantics**
   - 当前 `RecoveryPolicy._attempts` 是全局共享 budget；
   - 一次 Skill bypass tool failure、一次 no-progress、一次真实 test failure 会共同消耗 `4` 次总预算；
   - 需要设计 per-category / scoped recovery accounting，同时保留 global hard ceiling，避免互不相关 failure 随意吞掉彼此预算。

2. **Semantic progress**
   - 当前 no-progress 仍主要围绕 repository edit/test state；
   - 最终 E2E 中 `skill_load` + 新 verifier evidence 仍曾触发一次前期 `NO_PROGRESS`；
   - 至少 `skill_loaded`、`plan_created/plan_revised`、test state/evidence advancement 应能作为确定性的 semantic progress signal；
   - 不建议把“读到一个新文件”这种模糊信号直接视为 progress，避免无限探索被误奖励。

这些债务作为下一轮 **Planning v2 + Recovery v2 / Progress semantics** 处理，不回滚 Stage 1 DONE，也不提前混入 MCP / Skill Evolution。
