# Forge Agent Real-model Benchmark V1

日期：2026-09-22  
状态：**FORMAL V1 R2 COMPLETE / 48 REAL-MODEL TRIALS AGGREGATED**  
Benchmark ID：`forge-agent-real-model-benchmark-v1-r2`

## 1. 本轮事实源

Forge Agent：

~~~text
repository: Napabana/forge-agent
branch: dev
本轮开始 HEAD: 3962241d67566c752418eb0f24778e51b5fd6636
~~~

真实实验仓库：

~~~text
repository: Napabana/pr-test
原 main: f5ad77c739e21efcd65e6e6524f320e3325a96f7
forge-p2-mcp-demo: 23019998f2e801e79dea59fd23fc49c58fc20038
~~~

`forge-p2-mcp-demo` 相对原 `main`：

~~~text
ahead_by = 6
behind_by = 0
merge_base = 原 main
~~~

因此 2026-09-22 已将 `pr-test/main` 无冲突 fast-forward 到：

~~~text
23019998f2e801e79dea59fd23fc49c58fc20038
~~~

Benchmark V1 从这一 commit 冻结，不使用后续 main 漂移。

## 2. 纠正 synthetic draft

本轮最初曾基于 P2-0 Harness 的旧模型生成一版：

~~~text
task.files
→ 临时 tiny Git repo
→ Agent
~~~

这只能作为 synthetic Harness fixture，**不属于 Real-model Benchmark V1**。

发现问题时尚未执行任何 real-model trial，因此没有实验污染。

当前正式协议已经改为：

~~~text
pr-test @ frozen commit
        ↓
clone exact snapshot
        ↓
remove original .git history
        ↓
apply task-specific setup overlay
        ↓
git init + clean task baseline commit
        ↓
Agent trial
        ↓
independent deterministic acceptance
~~~

删除原 Git history 的原因是：部分 bug-fix task 的正确实现来自 source commit，如果保留历史，Agent 可以通过 `git show HEAD^` 看到 setup 前的正确答案，破坏 benchmark。

## 3. Source identity

正式 suite：

~~~text
evals/fixtures/coding_agent/benchmark_v1.json
~~~

source：

~~~yaml
repository: Napabana/pr-test
commit: 23019998f2e801e79dea59fd23fc49c58fc20038
~~~

canonical suite SHA-256：

~~~text
f0117c4ca39271401ba878c99afd30643b1dd7af28e0c0cdca9214f128b97f14
~~~

canonical hash 基于规范化 JSON，因此不受 key order、indent 或 CRLF/LF 影响。

## 4. A/B 归因边界

主实验只比较：

~~~text
baseline_react
  planning=off
  recovery=off
  skills=false
  MCP=false

vs

planning_recovery_skills
  planning=always
  recovery=structured
  skills=true
  MCP=false
~~~

保持相同：

- source repository；
- source commit；
- 12 个 task；
- task description；
- task setup；
- deterministic acceptance；
- model / Provider；
- native Tool registry；
- Repo Map mode；
- max_steps；
- Context budget；
- repetitions；
- test command；
- report protocol。

Full P2 不额外获得 steps/token budget。

MCP 不进入主实验。

## 5. Harness real-repository support

新增 suite-level source：

~~~json
{
  "source": {
    "repository": "Napabana/pr-test",
    "commit": "23019998f2e801e79dea59fd23fc49c58fc20038"
  }
}
~~~

新增 CLI：

~~~text
--source-repo <local pr-test clone>
~~~

`evals/coding_agent/runner.py` 对 external-source suite：

1. 校验本地 clone 中存在完整 frozen commit；
2. clone exact commit 到 trial repo；
3. 删除 clone 的 `.git`；
4. task.files 作为 setup overlay；
5. 重新 `git init`；
6. 将 setup 后状态提交为唯一 baseline commit；
7. Agent 只能看到 task 初始状态，看不到 source history；
8. trial patch 用 `git diff HEAD` 只记录 Agent 修改。

既有 P2-0 synthetic suite 仍使用原来的 `task.files → repo` 语义，不要求 external source。

## 6. Frozen 12 tasks

| Task ID | 主要类型 | 目标 | Recovery | Skill |
| --- | --- | --- | --- | --- |
| subtract-regression-recovery | 单文件 bug | 修复 subtract 回归 | 是，先复现 | should-trigger |
| batch-boundary-recovery | policy bug | 修复 max_batch_size 边界 | 是，先复现 | should-trigger |
| divide-float-regression-recovery | 单文件 bug | 恢复浮点除法和除零语义 | 是，先复现 | should-trigger |
| square-operation-feature | 单文件 feature | operations.square | 否 | should-not-trigger |
| registry-contains-feature | 单文件 feature | registry.contains | 否 | should-not-trigger |
| runtime-policy-mode-live | repository navigation | runtime 动态读取 config source-of-truth | 否 | should-trigger |
| power-operation-integration | 多文件 feature | operations/service/public API/facade/tests | replan candidate | should-trigger |
| registry-case-insensitive | registry contract | case/whitespace-insensitive lookup | 否 | should-trigger |
| policy-deny-list | policy feature | deny-list 优先于 allow-list | replan candidate | should-trigger |
| batch-stop-on-error | policy + service | stop_on_error 且默认行为不回归 | change-approach / replan candidate | should-trigger |
| custom-registry-preserve-overrides | registry + service | 保留 custom override，同时补齐 defaults | replan candidate | should-trigger |
| multiply-completion-guard | 单文件 bug + completion | 修复 multiply 且必须测试后完成 | completion guard | should-trigger |

覆盖：

- single-file bug fix；
- single-file feature；
- multi-file modification；
- repository navigation；
- test failure → repair；
- change-approach candidate；
- completion guard；
- replan candidate；
- Skill should-trigger；
- Skill should-not-trigger。

## 7. Task setup / hidden reference 边界

对 external-source suite：

~~~text
task.files
=
task-specific initial-state overlay
~~~

不是完整仓库文件集合。

`reference_files` 仅供 controller 在 real-model 调用前执行离线 reference validation。

Agent prompt 只得到 `task.description`，不会注入：

- reference_files；
- grader command；
- expected patch；
-最终实现。

当前 LocalRuntime 的 shell 不是完整 filesystem sandbox；因此本 Benchmark 的“hidden”含义是 controller-side、不注入 prompt / target repo，而不是对恶意 benchmark-hacking Agent 提供强对抗隔离。正式报告中不把它描述成 cryptographically secret hidden tests。

## 8. Scale

~~~text
12 tasks
× 2 repetitions
× 2 variants
= 48 real-model Agent trials
~~~

defaults：

~~~yaml
max_steps: 20
budget_tokens: 40000
repo_map_mode: incremental
MCP: false
~~~

注意：

`budget_tokens=40000` 是 Agent 每轮 Context TokenBudget，不是整条 run 的累计 Provider usage cap。

## 9. Primary metric

唯一主指标：

# Independent Acceptance Pass Rate

trial success：

~~~text
RunResult success
+
Acceptance passed / not_requested
+
all required deterministic outcome graders passed
~~~

报告：

~~~text
Baseline observed successes / 24
Full P2 observed successes / 24
observed success rate
absolute delta in percentage points
~~~

不称为稳定总体 pass@1。

## 10. Secondary metrics

效率优先统计 successful trials：

- mean / median steps；
- mean / median total Provider tokens；
- mean / median wall time。

全量 trial 指标另外保留用于审计。

subgroups：

- recovery-tagged success；
- Skill should-trigger selected/loaded rate；
- Skill should-not-trigger false-trigger rate；
- completion rejection；
- plan revision；
- recovery selected；
- test attempts；
- per-task baseline/full comparison；
- paired wins / losses / ties。

## 11. Report fairness gate

`scripts/report_coding_agent_benchmark_v1.py` 在聚合前强制检查 baseline/full：

~~~text
provider
protocol
model
suite_sha256
source_repository
source_commit
max_steps
budget_tokens
repo_map_mode
mcp_enabled
mcp_server_ids
~~~

另外验证 architecture mapping：

~~~text
baseline_react:
  planning=off
  recovery=off
  skills=false

planning_recovery_skills:
  planning=always
  recovery=structured
  skills=true
~~~

source commit、budget 或其他公平性字段发生漂移时直接拒绝生成正式 summary。

## 12. Deterministic regression

新增 / 更新：

~~~text
tests/test_coding_agent_benchmark_v1.py
~~~

覆盖：

- frozen source identity；
- task count / tag coverage；
- variant mapping；
- MCP 主实验关闭；
- external-source exact commit checkout；
- source history stripping；
- external reference validation contract；
- planned trial count；
- successful-only aggregation；
- fairness budget drift rejection；
- source commit drift rejection；
- output no-overwrite。

## 13. 当前离线验证状态

已完成：

- GitHub 远端确认 `pr-test/main == 23019998...`；
- suite JSON schema 静态检查；
- 12 task / tag coverage 静态检查；
- canonical hash 计算；
- variant/source/fairness 实现审查；
- 0 real Provider calls。

当前 ChatGPT 执行容器无法解析 `github.com`，因此完整仓库验证由用户本地执行。

2026-09-22 用户本地已经完成两个 0-API dry-run，结果：

~~~text
reference solutions: 12/12 PASS

baseline_react:
  execution_status: not_executed
  real_model_executed: false
  planned_trial_count: 24
  suite_sha256: f0117c4ca39271401ba878c99afd30643b1dd7af28e0c0cdca9214f128b97f14
  source_commit: 23019998f2e801e79dea59fd23fc49c58fc20038
  model: deepseek-v4.1-flash
  max_steps: 20
  budget_tokens: 40000
  planning: off
  recovery: off
  skills: false
  MCP: false

planning_recovery_skills:
  execution_status: not_executed
  real_model_executed: false
  planned_trial_count: 24
  suite_sha256: f0117c4ca39271401ba878c99afd30643b1dd7af28e0c0cdca9214f128b97f14
  source_commit: 23019998f2e801e79dea59fd23fc49c58fc20038
  model: deepseek-v4.1-flash
  max_steps: 20
  budget_tokens: 40000
  planning: always
  recovery: structured
  skills: true
  MCP: false
~~~

两组 dry-run 的公平性字段一致，差异只存在于预期的 Planning / Recovery / Skills capability。

当前尚未记录用户本地 `pytest tests/test_coding_agent_eval.py tests/test_coding_agent_benchmark_v1.py` 的结果，因此不能写成 pytest 已通过。

## 14. 本地验证顺序

先确保本地 `pr-test` clone 含 frozen commit：

~~~bash
cd /path/to/pr-test
git fetch origin
git rev-parse 23019998f2e801e79dea59fd23fc49c58fc20038
~~~

然后在 Forge Agent：

~~~bash
python -m pytest -q \
  tests/test_coding_agent_eval.py \
  tests/test_coding_agent_benchmark_v1.py
~~~

再运行 0-API dry-run。示例假定：

~~~text
SOURCE_REPO=/path/to/pr-test
~~~

Baseline：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --source-repo "$SOURCE_REPO" \
  --variant baseline_react \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_baseline_dry
~~~

Full P2：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --source-repo "$SOURCE_REPO" \
  --variant planning_recovery_skills \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_full_p2_dry
~~~

不加 `--real-model`，不得创建真实 Provider result。

只有 pytest + 两个 dry-run 全部确认后，才给出最终 real-model commands。

## 15. Final aggregation

真实 A/B 完成后：

~~~bash
python scripts/report_coding_agent_benchmark_v1.py \
  --baseline evals/results/benchmark_v1_baseline_real \
  --full-p2 evals/results/benchmark_v1_full_p2_real \
  --output-dir evals/results/benchmark_v1_summary
~~~

报告过程严格离线，不重新调用 Provider。

## 16. Evidence boundary

Benchmark V1 只能证明：

> 在固定 `pr-test@23019998...`、冻结的 12 个 task、同一 model/provider/budget 下，两种 Agent architecture 的 observed A/B 结果。

不能外推为：

- 稳定总体 pass@1；
- SWE-bench 成绩；
- 模型训练收益；
- MCP 收益；
- 对任意真实仓库的泛化成功率。

一旦真实运行开始，不根据结果改 suite / setup / grader / baseline / budget。

之后根据 bad case 修改 Agent 时，相应 case 进入 future regression/failure set，不再冒充同一份未经见过的 Benchmark V1 泛化证据。

## 17. Baseline real-model pilot audit（2026-09-22）

用户已完成并 push：

~~~text
variant: baseline_react
real-model trials: 24/24
model: deepseek-v4.1-flash
source: Napabana/pr-test@23019998f2e801e79dea59fd23fc49c58fc20038
suite_sha256: f0117c4ca39271401ba878c99afd30643b1dd7af28e0c0cdca9214f128b97f14
~~~

严格按当前 frozen grader / Run-Acceptance contract：

~~~text
observed successes: 18/24
observed success rate: 75.0%
completion_satisfied: 22
resource_exhausted(max_steps): 2
provider / connection / timeout failures: 0
~~~

successful-trial only：

~~~text
steps mean / median: 8.78 / 7.5
total tokens mean / median: 111286.8 / 72427.5
wall time mean / median: 77.22s / 59.68s
~~~

六个 strict failures：

1. `runtime-policy-mode-live` ×2：行为 grader PASS、full pytest PASS，但 required file grader 强制要求 reference 中的测试函数名 `test_policy_mode_reflects_runtime_config_changes`；Agent 实际加入等价 regression test `test_policy_mode_reads_current_config_value`，因此被判失败。
2. `custom-registry-preserve-overrides` ×2：行为 grader PASS、full pytest PASS，但 required file grader 强制要求 `OperationRegistry.contains()` helper；Agent 使用 `resolve()+UnknownOperationError` 完成相同用户可见契约，因此被判失败。
3. `power-operation-integration` ×2：核心 behavior 与 full pytest PASS，但 run 达到 `max_steps=20`，且 exact regression-test-name grader 未满足，因此按 Run/Acceptance contract 保持真实失败。

这说明当前 Benchmark V1 acceptance 含 implementation-shape coupling：部分 required file grader 检查了 reference solution 的具体 helper / test naming，而不是只检查 prompt 要求的 observable outcome。

因此：

- 当前 24 次 baseline artifact 保留，绝不删除或重写；
- 不把 18/24 单独解释为纯“任务功能完成率”；
- 不根据结果偷偷修改同一个 frozen suite 后继续称为同一 Benchmark V1；
- 在是否继续 Full P2 前先决定：保留 V1 strict metric 并明确限制，或者将当前 baseline 标记为 pilot、另建新 suite identity 后重新开始正式 A/B；
- 当前没有执行 Full P2 real-model trials。

## 18. Formal Benchmark V1 R2（pilot 后重新冻结）

用户明确同意在 pilot 后修正 benchmark 设计，并接受重新运行完整 48 trials。因此原 V1 24-trial baseline 不继续作为正式 A/B 的 baseline，而保留为 pilot evidence。

旧 pilot suite 已原样归档：

~~~text
evals/fixtures/coding_agent/benchmark_v1_pilot.json

suite_id:
forge-agent-benchmark-v1

canonical SHA-256:
f0117c4ca39271401ba878c99afd30643b1dd7af28e0c0cdca9214f128b97f14

defaults:
max_steps=20
budget_tokens=40000
~~~

正式 V1 revision 2：

~~~text
evals/fixtures/coding_agent/benchmark_v1.json

suite_id:
forge-agent-benchmark-v1-r2

canonical SHA-256:
f7ba1370fe1442773967ec4f65883be5cdc0a21f14ced28d1ea57ed1f384bd0a

source:
Napabana/pr-test@23019998f2e801e79dea59fd23fc49c58fc20038

defaults:
max_steps=30
budget_tokens=60000
~~~

### R2 acceptance correction

Pilot 暴露出的 implementation-shape coupling 已移除：

- 不再要求特定 regression-test 函数名；
- 不再要求任务 prompt 未指定的内部 helper；
- 正式 R2 的 required acceptance 中没有 `file` grader；
- API/功能通过 deterministic executable behavior grader 判断；
- repository regression 通过 full pytest 判断；
- prompt 明确要求新增回归测试的 6 个 task 使用通用 `test-change` command grader，只检查相对 trial baseline 是否真实修改 `tests/`，不限定测试文件、函数名或实现方式。

以下 6 个 task 要求 test change：

~~~text
runtime-policy-mode-live
power-operation-integration
registry-case-insensitive
policy-deny-list
batch-stop-on-error
custom-registry-preserve-overrides
~~~

### R2 budgets

~~~text
max_steps: 30
budget_tokens: 60000
~~~

原因：

- pilot 的 `power-operation-integration` 两次都在 step 20 达到 max_steps；
- `budget_tokens` 是每轮 Context TokenBudget，不是整条 trial 的累计 Provider usage cap；
- Full P2 会增加 Planning / Recovery / Skills context，因此给两种 variant 相同的 60k context cap，避免能力开关被过紧 context cap 干扰；
- 两个 variant 的预算仍完全相同。

### One-shot guarded runner

新增：

~~~text
scripts/run_coding_agent_benchmark_v1.py
~~~

它按固定顺序执行：

~~~text
source commit check
→ output no-overwrite check
→ deterministic pytest
→ baseline 0-API dry-run
→ Full P2 0-API dry-run
→ baseline 24 real trials
→ baseline artifact infrastructure health gate
→ Full P2 24 real trials
→ Full P2 artifact infrastructure health gate
→ offline final aggregation
~~~

health gate 只因 Provider / connection / runner infrastructure failure 停止；普通 task failure、Acceptance failure 或 max_steps failure 都保留为真实 benchmark outcome，不自动重跑。

正式输出目录：

~~~text
evals/results/benchmark_v1_r2_baseline_dry
evals/results/benchmark_v1_r2_full_p2_dry
evals/results/benchmark_v1_r2_baseline_real
evals/results/benchmark_v1_r2_full_p2_real
evals/results/benchmark_v1_r2_summary
~~~

任何目录已存在时 runner 拒绝覆盖，防止选择性重跑。

正式 R2 real-model trial 尚未由 ChatGPT 触发；必须由用户人工启动 one-shot runner。



## 19. Formal V1 R2 final result（2026-09-23）

用户已完成并 push 正式 R2 的全部 48 个 real-model trials，以及离线聚合产物：

~~~text
evals/results/benchmark_v1_r2_baseline_real/
evals/results/benchmark_v1_r2_full_p2_real/
evals/results/benchmark_v1_r2_summary/
~~~

公平性锚点：

~~~text
suite_id = forge-agent-benchmark-v1-r2
suite_sha256 = f7ba1370fe1442773967ec4f65883be5cdc0a21f14ced28d1ea57ed1f384bd0a
source = Napabana/pr-test@23019998f2e801e79dea59fd23fc49c58fc20038
model = deepseek-v4.1-flash
provider = openai
max_steps = 30
budget_tokens = 60000
repo_map_mode = incremental
MCP = off
repetitions = 2
~~~

Primary metric：

~~~text
baseline_react:
  23 / 24
  95.8%

planning_recovery_skills:
  23 / 24
  95.8%

absolute delta:
  0.0 percentage points

paired outcomes:
  Full P2 wins = 1
  Full P2 losses = 1
  ties = 22
~~~

Recovery-tagged tasks：

~~~text
baseline: 6/6
Full P2: 6/6
~~~

Successful-trial efficiency：

~~~text
steps mean:
  12.30 → 15.65
  Full P2 ≈ +27.2%

total tokens mean:
  169,602 → 223,881
  Full P2 ≈ +32.0%

wall time mean:
  102.02s → 118.32s
  Full P2 ≈ +16.0%
~~~

Process audit：

~~~text
Planning:
  plan_created = 24/24
  total plan steps completed = 61
  plan revisions = 0

Recovery:
  failure_classified = 20
  recovery_selected = 20
  recovery-tagged trials = 6 selections
  non-recovery-tagged trials = 14 selections
  recovery_replan = 0
  recovery_exhausted = 0

Skills:
  should-trigger trials = 20
  selected = 1
  loaded = 1
  loaded rate = 5%
  process match = 0/20
  should-not-trigger false-trigger = 0/4
~~~

两个正式失败均不是基础设施故障：

- baseline：`batch-stop-on-error/r2`，functional behavior/full suite 完成，但没有按 prompt 新增 regression-test change，因此 `test-change` acceptance 失败；
- Full P2：`power-operation-integration/r1`，同样因没有新增 regression-test change 而 acceptance 失败。

没有 provider / connection / timeout / runner infrastructure failure。

### 结论

这份冻结 12-task / 48-trial 小样本 **没有观察到 Full P2 对 independent acceptance 的提升**。

同时 Full P2 在成功 trial 上消耗更多 steps、tokens 与 wall time；Trace 表明当前 orchestration policy 的主要问题是：

1. Planning always-on 带来稳定开销，但本次没有发生 plan revision；
2. Recovery 对非 recovery-tagged task 存在明显过触发；
3. Skills 的 selection/adoption 极低，且唯一加载未命中该 task 的期望 Skill。

因此本轮不能写：

> Planning / Recovery / Skills 将 Coding Agent 成功率提升 X%。

可以准确写：

> 构建冻结 real-repository A/B Harness，并在 48 个 real-model trials 上验证两套 Agent architecture；结果显示 acceptance 持平（95.8% vs 95.8%），同时通过 Trace 定位到 always-on planning 开销、recovery 过触发和 skill adoption 不足，作为后续 orchestration policy 优化依据。

### 后续改进方向

后续若继续 P2，不修改本轮 frozen R2 结果。改动进入新 regression / benchmark revision：

- Planning：从 `always` 转向 task-complexity / mutation-risk gating，只有复杂 multi-file / ambiguity / recovery case 才建 plan；
- Recovery：缩紧 failure classification 与 selection gate，区分 expected test failure、normal exploration failure 和真正需要 strategy change 的 failure；
- Replanning：设计真正会触发 strategy invalidation 的任务/事件，验证 plan revision，而不是只记录初始 plan；
- Skills：改进 discovery/selection prompt 与 trigger metadata，优先验证 should-trigger adoption 和 wrong-skill rate；
- Evaluation：增加更多 repository/task diversity，再讨论总体 capability 趋势。
