# P2-5 Real-model Candidate Final Gate

日期：2026-09-21

状态：**IMPLEMENTED / LOCAL REGRESSION + DRY-RUN PENDING**

## 目标

在真实 Trace mining 已得到 evidence-ready recovery motif 后，只选择一个具有新增行为价值的 Candidate Skill 做最终 A/B。

本轮选择：

```text
failure:no_progress
→ recovery:change_approach
→ INSPECT
```

对应真实 pattern：

`pattern-591db3daf06f6bad`

选择原因：

- `tool_failure → inspect → INSPECT` 基本复述现有 `RecoveryPolicy`；
- `no_progress → change_approach → PLAN` 与已有 Planning/Recovery 控制高度重合；
- `no_progress → change_approach → INSPECT` 在现有 RecoveryPolicy 只规定“换方法”的基础上，补充了真实 accepted trajectories 中重复出现的下一步行为，因此值得进入真实 candidate evaluation。

## 执行入口

新增：

`scripts/run_real_skill_evolution_eval.py`

默认是 dry-run：

- 不读取 provider credentials；
- 不创建 backend；
- 不调用真实模型；
- 只校验 mining report、candidate identity/hash、suite reference solution；
- 输出 planned paired trials 与 ordered process grader。

只有显式 `--execute` 才运行真实模型。

## 最终 Gate 结构

默认 PromotionGate 仍要求四种 evaluation role：

```text
TARGET
SHOULD_TRIGGER
SHOULD_NOT_TRIGGER
NON_REGRESSION
```

每种 role 都跑 baseline / candidate，因此总计：

```text
4 roles × 2 variants × 1 repetition = 8 real Agent trials
```

没有降低 PromotionGate 的 role 要求。

## 专项 suite

新增：

`evals/fixtures/skill_evolution/real_recovery_motif_suite.json`

target / should-trigger：

- Planning = off；
- Recovery = structured；
- Repo Map = none；
- no-progress threshold = 2；
- 任务明确要求先检查 implementation + contract 两份证据；
- 目的是稳定产生 `no_progress → change_approach`，测试 Candidate 是否在该 recovery 之后被加载。

should-not-trigger / non-regression：

- no-progress threshold = 6；
- 不人为制造 recovery trigger；
- 验证 Skill 不应错误触发，以及普通能力不退化。

所有 trial 只暴露最小工具集：

- file_read
- file_view
- search_text
- find_files
- find_symbol
- file_edit
- file_write
- test

关闭 shell、git tool、MCP、Repo Map，减少无关 schema/token 开销。

## Ordered Recovery Motif Grader

新增 grader kind：

`recovery_motif`

文件：

- `evals/coding_agent/schema.py`
- `evals/coding_agent/graders.py`
- `evals/coding_agent/runner.py`

candidate target/should-trigger 必须观察到严格顺序：

```text
failure_classified(no_progress)
→ recovery_selected(change_approach)
→ skill_loaded(candidate)
→ next semantic action = INSPECT
```

grader 不允许：

- Skill 在 recovery 前提前加载；
- 跨下一次 failure/recovery 拼接 motif；
- Skill 加载后第一个语义动作不是 INSPECT。

`recovery_motif` 被定义为 process grader，不参与 Acceptance 的 outcome verifier；只有 RunResult/Trace 已生成后才评估。

## EvaluationRecord / PromotionGate

`experience/evaluation.py` 新增：

- candidate-specific process graders；
- real-model run metadata；
- `candidate_process_passed` 同时包含 skill-selection 与 recovery-motif process checks。

真实执行结束后自动生成：

- baseline/candidate P2-0 reports；
- `evaluation.json`；
- `promotion_decision.json`；
- `final_gate_summary.json`。

会运行现有 `PromotionGate`，但：

**不会自动 promotion。**

即使 status=PASS，也保留人工检查后再决定是否写入正式 `.agents/skills/`。

## Claim boundary

这套 final gate 是 targeted behavioral evaluation：

- source evidence 来自真实 accepted coding-agent trajectories；
- A/B 使用真实模型；
- trigger 条件在 dedicated eval runner 中通过较低 no-progress threshold 稳定制造；
- 因此它证明的是“在指定 recovery trigger 下，这个 mined Skill 是否能被正确选择并保持结果/开销约束”，不是对所有 coding tasks 的普遍性能提升声明。

## 本地验证

先运行：

```bash
python -m pytest -q \
  tests/test_coding_agent_eval.py \
  tests/test_skill_evolution.py \
  tests/test_real_skill_evolution_driver.py \
  tests/test_real_skill_evolution_eval.py
```

然后对真实 Pattern 4 做 0-API dry-run：

```bash
python scripts/run_real_skill_evolution_eval.py \
  --mining-report /mnt/e/2806/forgeAgent/forge-agent/evals/results/skill-evolution-mine-20260921-121439/mining_report.json \
  --pattern-id pattern-591db3daf06f6bad \
  --output-dir /mnt/e/2806/forgeAgent/forge-agent/evals/results/p2-5-pattern4-final-gate
```

确认 dry-run 输出：

- `provider_calls: 0`
- `paired_trials: 8`
- pattern signature 为 `no_progress → change_approach → INSPECT`
- process grader required=true
- reference validation 全部通过。

之后只有用户手工追加 `--execute` 才进入真实模型评测。
