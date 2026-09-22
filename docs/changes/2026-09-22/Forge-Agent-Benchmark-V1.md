# Forge Agent Real-model Benchmark V1

日期：2026-09-22  
状态：**REAL-REPOSITORY PROTOCOL IMPLEMENTED / 12/12 REFERENCE + BOTH DRY-RUNS PASSED / PYTEST RESULT NOT YET RECORDED**  
Benchmark ID：`forge-agent-real-model-benchmark-v1`

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
