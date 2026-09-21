# P2-5 真实 Trace Mining Driver

日期：2026-09-21

状态：**IMPLEMENTED / LOCAL REAL-TRACE RUN PENDING**

## 背景

此前 `scripts/run_pr_test_practice_batch.py` 已在用户本地完成真实 Provider 运行，最终得到三个 accepted + P2-5 eligible Trace v2：

- Task A：success / acceptance=passed / P2-5 eligible
- Task B：success / acceptance=passed / P2-5 eligible
- Task C：success / acceptance=passed / P2-5 eligible

最终成功 batch summary：

- total steps: 84
- total tokens: 1,227,602
- total elapsed: 1439.1s
- Task B: 31 steps / 381,712 tokens / 692.2s
- Task C: 32 steps / 602,226 tokens / 488.4s

运行中出现过一次 LLM timeout 与一次 connection error，均由现有 bounded retry 恢复，最终 B/C 仍通过 independent acceptance。

这证明真实 accepted trajectories 已经准备好，可以进入 P2-5 的离线 experience mining；本轮不继续生成更多真实 coding tasks。

## 新增入口

新增：

`scripts/run_skill_evolution.py`

目标是把现有 P2-5 library API 变成一个安全、可观察、0 API 的真实 Trace 入口。

当前入口**只允许显式 `--mine-only`**，不会：

- 创建 LLM backend；
- 调用 DeepSeek/OpenAI/Anthropic/KRILL；
- 运行 EvaluationHarness；
- 自动执行 candidate A/B；
- 自动 promotion。

## 输入

支持两种输入方式，可以组合：

1. 重复传入 Trace：

```text
--trace /path/to/a.jsonl
--trace /path/to/b.jsonl
```

2. 直接传 Batch Runner 的 summary：

```text
--batch-summary /path/to/batch_summary.json
```

此时 driver 读取：

`eligible_traces_for_p2_5`

并自动去重、解析对应 Trace v2。

## 执行链

```text
Trace v2 / batch_summary.json
        ↓
load_trajectory
        ↓
eligibility normalization
        ↓
ExperienceMiner
        ↓
exact typed workflow grouping
        ↓
DeterministicCandidateGenerator
        ↓
CandidateStore（默认）
        ↓
mining_report.json + candidate SKILL.md
```

这里不会把“语义相近”的任务强行合并。

当前 `ExperienceMiner` 的真实 contract 是：

- successful workflow：按完整 typed `workflow` signature 精确分组；
- recovery workflow：v2 改为 `failure category + recovery strategy + first semantic action` 的 bounded typed motif 分组；
- ineligible trajectory 不进入 pattern；
- 同一 source evidence 由既有 schema 去重。

因此 A/B/C 是否共享 recovery evidence，只由真实落盘的 typed failure/recovery/semantic-action motif 决定；driver 不做自然语言猜测。完整 workflow 仍保留在 report provenance 中。

## Candidate / evidence gate

每个 mined pattern 都调用现有：

`DeterministicCandidateGenerator.generate(pattern)`

生成标准 P2-3 `SKILL.md` candidate。

driver 同时读取当前：

`PromotionGateConfig().min_evidence_count`

默认值为 2，并在报告中标记：

`promotion_evidence_ready: true/false`

这个标记只表示 source evidence count 是否达到默认 promotion gate 的最低证据数，不代表 candidate 已通过 evaluation，也不代表可 promotion。

如果所有 candidate 都低于该门槛，CLI 会明确提示：

`Do not spend real-model evaluation tokens yet.`

## CandidateStore

默认使用既有：

`<target repo>/.forge-agent/experience/`

保持 candidate 与正式：

`<target repo>/.agents/skills/`

物理隔离。

重复对同一 trace evidence 运行时：

- candidate identity/version/hash 相同；
- 不重复覆盖；
- driver 读取并复用既有 candidate；
- report 标记 `candidate_store_reused=true`。

可使用：

`--no-store`

仅生成 report artifact，不写 CandidateStore。

## 输出

默认：

`evals/results/skill-evolution-mine-<UTC>/`

包含：

```text
mining_report.json
candidates/
  <skill-name>/
    candidate.json
    SKILL.md
```

`mining_report.json` 保存：

- provider_calls = 0
- evaluation_executed = false
- input trace refs
- normalized eligibility
- workflow
- failure/recovery signals
- pattern id/type/signature/evidence count
- candidate id/version/hash
- CandidateStore path / reused 状态
- promotion minimum evidence threshold
- promotion_evidence_ready

## 测试

新增：

`tests/test_real_skill_evolution_driver.py`

覆盖：

- 从 `batch_summary.json` 读取 eligible traces；
- fixture 中两条相同 typed workflow 合并成 evidence_count=2；
- candidate 达到默认 source-evidence gate；
- 单证据 candidate 明确标记为 not gate-ready；
- ineligible/canceled trace 可见但不进入 mining；
- CandidateStore 对同一 evidence 重跑幂等；
- 必须显式 `--mine-only`。

本窗口没有真实执行用户本地三条 Trace；最终 pattern/candidate 数量必须由用户本地运行结果决定。

## 下一步

用户本地 pull 最新 `dev` 后：

1. 运行 driver 专项测试；
2. 对已完成 Batch Runner 的 `batch_summary.json` 执行一次 `--mine-only`；
3. 把终端输出与 `mining_report.json` 带回；
4. 只有存在合理且 evidence-ready 的 candidate，才设计下一条显式 real-model baseline/candidate evaluation 命令。

在第 4 步之前不再消耗真实 Provider API。


## Mining strategy v2

真实首轮 mining 暴露 full recovery signature 过严后，recovery mining 已升级为 `recovery_motif_v2`；详见 `docs/changes/2026-09-21/P2-5-Recovery-Motif-Mining-v2.md`。
