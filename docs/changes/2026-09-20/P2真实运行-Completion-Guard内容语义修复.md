# P2 真实运行：Completion Guard 内容语义修复

日期：2026-09-20  
问题发现来源：P2 Planning + Recovery + Skills 真实模型运行  
问题修复提交：`3ac800195b9162186c0f38815903442437d75eab`  
状态：**FIXED / LOCAL REGRESSION PASSED**

## 1. 真实运行现象

在 `pr-test` 的受控回归任务中，Agent 已完成实际功能修复并通过测试，但第一次 FINISH 后继续运行，最终被与业务无关的 Windows/WSL 换行符噪声拖入 Recovery budget exhaustion。

关键执行链：

```text
pytest failure
  → test_failure
  → recovery: inspect

plan_create
  → 两次 schema/validation rejection
  → plan_created v1

calculator.py 修复
  → focused pytest passed
  → full pytest passed
  → git add
  → git commit

FINISH
  → FINAL_STATE_UNVERIFIED
  → recovery: rerun_test

后续 CRLF/LF working-tree 噪声
  → permission_denied
  → change_approach
  → no_progress
  → replan
  → plan_revised v2
  → permission_denied
  → recovery_exhausted
  → INCOMPLETE
```

真实 Trace run_id：

```text
251f95d5_20260920_070535
```

其中 Step 15 明确记录：

- `completion_rejected.code = FINAL_STATE_UNVERIFIED`
- `recovery_selected.strategy = rerun_test`

因此问题不是 RecoveryPolicy 自身错误，而是 Completion Guard 对“测试后仓库是否变化”的语义过粗。

## 2. 根因

修复前，`agent/core.py` 使用同一个 `repository_fingerprint()` 同时承担：

1. Loop / progress 检测；
2. require_changes 判断；
3. 成功测试后的 final-state freshness 判断。

而 `repository_fingerprint()` 包含：

```text
HEAD SHA + working-tree fingerprint
```

因此：

```text
pytest PASS
→ git add
→ git commit
```

即使 checkout 中的文件字节完全没变，commit 仍会改变 HEAD，导致 Completion Guard 误判“测试后代码又变了”，触发 `FINAL_STATE_UNVERIFIED`。

这里混淆了两个不同问题：

- “Git 仓库状态有没有变化？”
- “测试实际观察过的文件内容有没有变化？”

## 3. 修复设计

保留原 `repository_fingerprint()`，继续服务：

- Loop detection；
- No-progress / repository progress；
- Repo Map 更新与 Git-aware 状态观察。

新增：

```python
repository_content_fingerprint(repo_path)
```

它基于 checkout-visible 文件内容构造稳定指纹：

```text
git ls-files --cached --others --exclude-standard
  ↓
path
+ file bytes
+ executable bit
  ↓
content fingerprint
```

故意不包含：

- HEAD SHA；
- staged / unstaged 状态；
- commit metadata。

Completion Guard 现在单独记录：

- 初始 content state；
- 最近一次成功测试时 content state；
- FINISH 时 final content state。

最终 freshness 判断变为：

```text
last_successful_test_content_state
        ==
final_repo_content_state
```

因此：

```text
test PASS
→ git add
→ git commit
→ content 未变化
→ FINISH allowed
```

但：

```text
test PASS
→ file_write
→ content 变化
→ FINISH rejected: FINAL_STATE_UNVERIFIED
```

原有安全边界保持不变。

## 4. 可观察性修复

本次真实运行还暴露了 CLI 可观察性缺口。

旧 `RunEventRenderer` 只显示 Action / Observation 等事件，导致终端呈现：

```text
[Step 15] action=finish
[Step 16] action=test
```

却隐藏中间真正发生的：

```text
completion_rejected
→ recovery_selected
```

现在终端会直接显示关键控制事件：

- `PLAN_CREATED`
- `PLAN_REVISED`
- `PLAN_REJECTED`
- `SKILL_LOADED`
- `COMPLETION_REJECTED`
- `RECOVERY_SELECTED`
- `RECOVERY_EXHAUSTED`

这样 `agent run` 不需要依赖模型自由文本 reasoning，也可以直接理解控制流。

## 5. 修改范围

生产代码：

- `context/repository_state.py`
  - 新增 `repository_content_fingerprint()`
- `agent/core.py`
  - Completion Guard 从 step-order freshness 改为 content-state freshness
  - 保留 Git-aware repository state 给 progress / loop 路径
- `entry/event_renderer.py`
  - 输出关键 Planning / Recovery / Completion 控制事件

回归测试：

- `tests/test_agent_completion_guards.py`
  - 新增 `test_git_commit_after_successful_test_does_not_require_retest`
- `tests/test_event_renderer.py`
  - 新增关键 control-flow event renderer 测试

## 6. 本地验证

用户在拉取修复后执行以下专项回归：

```bash
python -m pytest -q \
  tests/test_agent_completion_guards.py \
  tests/test_event_renderer.py \
  tests/test_structured_recovery.py
```

用户确认全部通过。

未提供该轮完整 pytest stdout、passed 数量与耗时，因此日志不补造统计数字。

## 7. Evidence boundary

本次修复可以支持：

- Completion Guard 已区分 Git metadata state 与 checkout content state；
- 单纯 `git add / git commit` 不再使已通过测试的相同文件内容失效；
- 测试后真正修改文件仍会触发 `FINAL_STATE_UNVERIFIED`；
- CLI 能直接观察 Planning / Recovery / Completion 关键控制事件。

本次修复不能支持：

- Recovery 提高真实任务成功率 X%；
- Planning / Skills 提升 pass@1；
- token / latency / step 数量平均下降；
- 所有 Windows/WSL CRLF 问题均已解决。

这些需要后续重新执行真实 Stage 1 与正式 Evaluation Harness 才能形成证据。
