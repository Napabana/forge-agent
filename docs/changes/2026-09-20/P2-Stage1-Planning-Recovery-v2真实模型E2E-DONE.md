# P2 Stage 1：Planning v2 / Recovery v2 真实模型 E2E（DONE）

日期：2026-09-20

状态：**DONE**

Forge Agent 基线：`dev@d41bd7a1cca76014bdd12c42029e4ae20f212aea`

测试仓库：`Napabana/pr-test`

测试分支：`forge-p2-skill-demo`

模型：`deepseek-v4.1-flash`

Provider 路径：OpenAI-compatible

## 1. 最终结果

用户执行真实模型 E2E，最终：

```text
Status  : SUCCESS
Steps   : 15
Tokens  : 89,485
Time    : 89.0s
Acceptance: not_requested
```

任务要求：

```text
Fix the normalize_label regression with the smallest correct change.
Follow the repository-specific release verification contract.
Do not create a Git commit.
```

最终修改：

- 仅修改 `label_utils.py`
- 将单空格 `replace(" ", "-")` 改为基于正则的连续 whitespace collapse
- 未创建 Git commit

验证结果：

```text
focused pytest: 4 passed
full pytest: 19 passed
repository verifier: release contract: OK
```

## 2. 真实执行链

终端可见执行链：

```text
Step 1  skill_load verify-release-contract
        ↓
Step 2  read label_utils.py
        ↓
Step 3  read tests/test_label_utils.py
        ↓
Step 4  read scripts/verify_release_contract.py
        ↓
Step 5  plan_create
        ↓
Step 6  focused pytest FAIL
        ↓
        failure_classified(test_failure)
        ↓
        recovery_selected(inspect, 1/4)
        ↓
Step 7  file_write label_utils.py
        ↓
Step 8  focused pytest PASS (4 passed)
        ↓
Step 9  full pytest PASS (19 passed)
        ↓
Step 10 plan_step_update
        ↓
Step 11 repository verifier PASS
        ↓
Step 12 plan_step_update
Step 13 plan_step_update
Step 14 plan_step_update
        ↓
Step 15 FINISH
        ↓
SUCCESS
```

模型先完成 repository-specific Skill 加载与只读检查，再创建 plan，没有在没有仓库证据时直接 mutation。

## 3. Planning v2 Trace 证据

用户提供的过滤 Trace：

```text
skill_loaded           {'step_id': 1, 'already_loaded': False}
plan_created           {'step_id': 5, 'state_changed': True}

plan_step_completed    {
  'step_id': 'fix-normalize-label-to-normalize-all-whitespace-to-kebab-case',
  'plan_version': 1,
  'step_status': 'completed',
  'state_changed': True,
  'idempotent': False
}
plan_step_completed    {
  'step_id': 'run-focused-pytest-file',
  'plan_version': 1,
  'step_status': 'completed',
  'state_changed': True,
  'idempotent': False
}
plan_step_completed    {
  'step_id': 'run-full-pytest-suite',
  'plan_version': 1,
  'step_status': 'completed',
  'state_changed': True,
  'idempotent': False
}
plan_step_completed    {
  'step_id': 'run-release-contract-verifier',
  'plan_version': 1,
  'step_status': 'completed',
  'state_changed': True,
  'idempotent': False
}
```

### 3.1 Runtime-owned identity 已被真实模型验证

四个 step id 都是由 Runtime 根据语义 description 生成的稳定 slug：

1. `fix-normalize-label-to-normalize-all-whitespace-to-kebab-case`
2. `run-focused-pytest-file`
3. `run-full-pytest-suite`
4. `run-release-contract-verifier`

模型后续通过这些 Runtime-owned identity 更新 plan state。

这证明本轮真实 E2E 不再依赖模型自行构造 `id/status/version` bookkeeping。

### 3.2 本次没有 idempotent duplicate

四个 `plan_step_completed` 都是：

```text
state_changed = true
idempotent = false
```

因此它们都是首次真实状态转换，而不是重复确认。

本次 real-model run **没有覆盖**：

```text
completed → completed
state_changed=false
idempotent=true
```

该行为已经由 deterministic regression 覆盖，但不得把它写成这次 real-model evidence。

### 3.3 无可见 malformed planning control

用户提供的过滤 Trace 中没有：

```text
plan_rejected
```

终端执行也没有出现旧 Stage 1 run 中的 malformed planning call 提示。

因此这次 single run 可以确认：

- `plan_create` 正常；
- 4 个 Runtime step identity 均可被后续 update 使用；
- 没有可见 plan rejection。

不能从一次 run 推导 malformed rate 已长期归零。

## 4. Recovery v2 Trace 证据

用户提供 Trace：

```text
failure_classified {
  'step_id': 6,
  'category': 'test_failure'
}

recovery_selected {
  'step_id': 6,
  'category': 'test_failure',
  'category_occurrence': 1,
  'category_max_attempts': 4,
  'global_attempt': 1,
  'global_max_attempts': 12
}
```

这证明 real-model runtime 已进入新的两层 Recovery accounting：

```text
test_failure category budget: 1 / 4
global hard ceiling:          1 / 12
```

本次没有其它 failure category，因此没有用 real-model run 同时证明两个 category 互不侵占预算；该隔离行为由 deterministic regression 覆盖。

本次也没有：

- recovery exhaustion
- plan revision
- infrastructure failure

所以这些行为仍不能作为本次 real-model evidence。

## 5. Skill / Semantic Progress / Completion

Skill：

```text
skill_loaded:
  already_loaded = false
```

说明 project Skill 正常进入 runtime context。

实际 workflow 与 Skill contract 一致：

1. focused pytest
2. full pytest
3. repository-specific verifier

本次没有 duplicate Skill load，因此 duplicate-load semantic-progress contract 仍以 deterministic test 为主。

最终所有验证都通过后才 FINISH，没有可见 completion rejection。

## 6. Stage 1 Before / Planning-Recovery v2 After

旧 Stage 1 真实模型 run：

```text
run_id: ff98262c_20260920_121748
status: SUCCESS
steps: 18
tokens: 122,597
wall time: 98.6s
```

本次 v2 run：

```text
status: SUCCESS
steps: 15
tokens: 89,485
wall time: 89.0s
```

观察差异：

| 指标 | Stage 1 old | Planning/Recovery v2 | 单次差值 |
| --- | ---: | ---: | ---: |
| Status | SUCCESS | SUCCESS | — |
| Steps | 18 | 15 | -3 |
| Tokens | 122,597 | 89,485 | -33,112 |
| Wall time | 98.6s | 89.0s | -9.6s |
| Focused test | PASS | 4 passed | PASS |
| Full test | 19 passed | 19 passed | 持平 |
| Repository verifier | OK | OK | 持平 |
| Visible plan rejection | 旧 run 出现过 malformed control | 本次未见 | 单样本改善 |
| Runtime-owned step id | 旧 contract 不完整 | 4 个稳定 Runtime ids | 已观察 |
| Recovery accounting | 旧全局小预算 | category 1/4 + global 1/12 | 已观察 |

这只是 **同一 fixture 上的两个真实模型样本**。

允许写：

- 本次 v2 run 成功；
- 本次 run 的 steps/tokens/time 比旧 run 更低；
- 本次没有可见 malformed planning control；
- Runtime-owned step ids 和 category/global Recovery accounting 在真实链路中工作。

不允许写：

- token efficiency 稳定提升 27%；
- latency 稳定提升约 10%；
- success rate 提升；
- pass@1 提升；
- malformed planning call 已彻底消失；
- Recovery effectiveness 已有统计显著提升。

这些都需要多次重复实验或正式 A/B。

## 7. 最终证据边界

截至本次 E2E：

```text
Deterministic regression                         DONE
Runtime-owned Planning identity                  REAL-MODEL VERIFIED
Dynamic plan execution                           REAL-MODEL VERIFIED
Four real plan step transitions                  REAL-MODEL VERIFIED
Idempotent duplicate terminal update             DETERMINISTIC ONLY
Per-category/global Recovery accounting          REAL-MODEL VERIFIED
Cross-category recovery budget independence      DETERMINISTIC ONLY
Skill load + repository workflow                 REAL-MODEL VERIFIED
Semantic-progress duplicate handling             DETERMINISTIC ONLY
Completion + focused/full/repo verification      REAL-MODEL VERIFIED
Overall performance improvement                  NOT CLAIMED
```

Planning v2 / Recovery v2 的本轮基础设施债务至此完成 deterministic + real-model 双层收口。
