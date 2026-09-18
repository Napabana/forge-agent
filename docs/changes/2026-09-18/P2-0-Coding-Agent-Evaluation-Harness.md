# P2-0 Coding Agent Evaluation Harness

日期：2026-09-18  
实现基线：\`dev@a9c745f77db4adae4818f381261a2dd4c151e552\`  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 目标

P2-0 建立通用 task-level Coding Agent Evaluation Harness，作为后续 Planning / Recovery / Skills 等 architecture variant 的统一 A/B 入口。评测层不实现第二套 Agent loop，正式执行链仍然是：

\`\`\`text
EvaluationSuite
  ↓
EvalTask
  ↓
Trial
  ↓
ExecutionRunner
  ↓
Agent
  ↓
ToolExecutor
  ↓
RunResult + Trace
  ↓
Deterministic Graders
  ↓
TrialResult
  ↓
EvalReport
\`\`\`

Evaluation Harness 只负责 case、隔离环境、trial、grading、artifact 和 aggregation；Agent Harness 保持现有生产实现。

## 实现

新增 \`evals/coding_agent/\`：

- \`schema.py\`：\`EvalTask\`、\`TrialConfig\`、\`TrialResult\`、\`GraderResult\`、\`EvalReport\` 等 schema 与校验。
- \`runner.py\`：materialize 干净临时 Git repository；调用生产 \`ExecutionRunner\`；执行隐藏 deterministic verifier；保存 patch、final state、trace reference、trial artifact；异常仍生成 \`TrialResult\`。
- \`graders.py\`：command / file / repository_state / run_trace grader，以及 RunResult/Trace metrics extraction。
- \`report.py\`：按 evidence level 聚合结果。fake/scripted trial 不输出 capability pass rate；只有显式 real-model trial 才进入 observed small-sample metrics。
- \`__main__.py\`：正式 CLI，支持 suite、variant、repetitions、output-dir、task filter 和显式 \`--real-model\`。

没有修改 \`agent/core.py\`、\`harness/executor.py\` 或 Trace v2 schema。

## 首版 suite

\`evals/fixtures/coding_agent/suite.json\` 固定 8 个小任务：

1. simple single-file feature
2. simple bug fix
3. test regression
4. multi-file change
5. repository navigation
6. completion/test requirement
7. recoverable test failure
8. final-state verification

每个 case 都带 reference solution，用 deterministic outcome graders 做 fixture 自检。hidden grader 不进入 Agent prompt。

## Grader 与 metrics

首版 deterministic graders：

- command/test grader：参数列表直接执行，不经 shell。
- file grader：exists / contains / not_contains / equals。
- repository-state grader：基于 \`git status --porcelain\` 判断变更。
- run/trace grader：RunStatus、termination reason、required tool、test/tool/completion-rejection/reflection count。

统一抽取：

- success / acceptance / RunStatus / termination reason
- steps / total tokens / provider usage / wall time
- tool calls / test attempts / completion rejections
- reflections / file reads / shell calls
- patch / final state / trace reference

过程 grader 只用于明确要求行为证据的 case；普通 coding task 仍以最终 outcome 为主要成功标准。

## Evidence boundary

fake / MockBackend / scripted backend 只验证 Evaluation Harness 自身：

\`\`\`text
deterministic harness trial
→ schema / isolation / runner integration / grader / report correctness
\`\`\`

它们不会生成 \`observed_success_rate\`。聚合报告会明确写：

\`\`\`text
pass_rate_intentionally_omitted = true
\`\`\`

真实模型必须显式使用 \`--real-model\`。没有 provider credential 时，CLI 生成：

\`\`\`text
execution_status = not_executed
real_model_executed = false
\`\`\`

不会制造 trial 或 capability 数字。

本轮冻结占位 artifact：

\`evals/results/coding_agent_baseline_not_executed/\`

其中 baseline variant 为 \`baseline_react\`，计划 8 个 trial，实际 real-model trial 为 0。

## 输出与 repetition

trial id 稳定为：

\`\`\`text
<task_id>--<variant>--r<NNN>
\`\`\`

因此未来可以直接表达：

\`\`\`text
task × architecture variant × repetition
\`\`\`

输出目录要求不存在；已有目录默认抛出 \`FileExistsError\`，不静默覆盖 frozen result。

## 与 Failure Harness 的关系

P1 Failure Harness 保持原职责：

\`\`\`text
deterministic failure injection
→ regression contract
\`\`\`

P2-0 Evaluation Harness 是：

\`\`\`text
coding task
→ isolated trial
→ production Agent
→ outcome + trajectory
→ graders + metrics
\`\`\`

Evaluation case 可以要求先复现测试失败再修复，但本轮没有引入新的 Failure Injection 或 RecoveryPolicy 抽象。

## 回归覆盖

新增 \`tests/test_coding_agent_eval.py\`，覆盖：

- EvalTask schema 与 duplicate task id rejection
- stable Trial id
- clean isolated fixture
- grader success / failure / multi-grader aggregation
- Agent failure 仍生成 TrialResult
- Trace / RunResult metrics extraction
- report aggregation
- output directory no-overwrite
- no-provider / not-executed
- fake backend 不被报告为 real-model evidence
- task selection / repetition
- 8-case reference solution self-check

## 本轮实际验证

当前执行环境无法通过网络取得可执行 GitHub checkout，因此没有把静态检查冒充 pytest 通过。

已实际执行：

\`\`\`text
python -m py_compile evals/coding_agent/*.py tests/test_coding_agent_eval.py
\`\`\`

草稿 Python 文件静态编译通过；suite JSON 可解析为 8 个 task；纯 schema smoke 检查验证了 8-task load、稳定 repetition id 与 duplicate task rejection。

尚未实际执行仓库级 pytest、Docker E2E 或真实模型 experiment。

## 本地验证命令

拉取提交后执行：

\`\`\`bash
python -m pytest -q tests/test_coding_agent_eval.py

python -m evals.coding_agent \
  --output-dir evals/results/local-p2-0-not-executed

python -m pytest -q \
  tests/test_coding_agent_eval.py \
  tests/test_runner.py \
  tests/test_failure_harness.py \
  tests/test_failure_harness_isolate.py \
  tests/test_trace_v2.py \
  tests/test_agent_completion_guards.py \
  tests/test_evidence_pack.py \
  tests/test_repo_map_agent_ablation.py

python -m evals.verify_evidence_pack

python -m pytest -q
\`\`\`

第二条命令必须使用一个当前不存在的输出目录，以验证 no-overwrite 语义。

## 本轮明确未做

Planning、Replanning、Failure-aware RecoveryPolicy、Skills、MCP、trajectory-driven evolution、Multi-Agent、LLM-as-Judge 主链、完整 SWE-bench compatibility 和大规模付费 benchmark 均未实现。

下一阶段 P2-1 可以在不改变本 Harness 主链的前提下新增 \`planning\` variant，与 \`baseline_react\` 做相同 suite / repetition 协议的对比。
