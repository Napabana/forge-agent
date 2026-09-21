# P2-5 v2：Recovery Motif Mining

日期：2026-09-21

状态：**IMPLEMENTED / LOCAL REAL-TRACE RE-RUN PENDING**

## 触发原因

用户对 3 条真实 accepted + P2-5 eligible trajectories 执行首版 `--mine-only` 后得到：

- 3 eligible traces
- 3 recovery patterns
- 每个 pattern 的 evidence_count 都为 1
- 0 个 candidate 达到默认 `PromotionGateConfig.min_evidence_count=2`

真实输出表明问题不在 eligibility，也不应该通过降低 evidence gate 解决，而在首版 recovery mining 的 grouping key 过于严格。

旧实现把 recovery pattern 定义为：

```text
all failure categories
+ all recovery strategies
+ full normalized workflow
```

因此只要两条真实 run 在任意位置多一次 INSPECT、不同次数 test failure、不同恢复升级顺序，整条 signature 就不同。

另外，failure/recovery 信息同时出现在 signature 前缀和 workflow 中，存在重复编码。

## v2 设计

successful workflow 保持原行为：

```text
full normalized workflow
→ exact deterministic grouping
```

recovery workflow 改为 bounded local motif：

```text
failure_classified(category)
        ↓
recovery_selected(strategy)
        ↓
first semantic action after recovery
```

例如：

```text
failure:test_failure
→ recovery:inspect
→ INSPECT
```

或：

```text
failure:no_progress
→ recovery:change_approach
→ PLAN
```

实现位置：

`experience/trajectory.py`

新增内部：

`_recovery_motifs(workflow)`

当前 mining strategy 显式版本化为：

`recovery_motif_v2`

并写入真实 mining report：

```json
{
  "mining_strategy": "recovery_motif_v2"
}
```

## 关键边界

### 1. 不降低 PromotionGate

仍保持：

`PromotionGateConfig.min_evidence_count = 2`

v2 的目标是更准确地定义“同一种可复用经验”，不是把单条 evidence 直接当成可 promotion candidate。

### 2. 同一 trace 对同一 motif 只贡献一份 evidence

一条 run 内即使重复发生：

```text
test_failure → inspect → INSPECT
test_failure → inspect → INSPECT
```

同一 motif 不会因此伪造 evidence_count=2。

最终 evidence 仍由不同 source trajectory identity 聚合。

### 3. 保留 recovery escalation

同一个 failure 到下一个 failure 之间可以出现多个 recovery strategy。

例如：

```text
FAIL:test_failure
RECOVER:inspect
INSPECT
RECOVER:replan
REPLAN
```

会分别产生：

```text
failure:test_failure → recovery:inspect → INSPECT
failure:test_failure → recovery:replan → REPLAN
```

不会因为只保留第一次 recovery 而丢失升级经验。

### 4. full workflow 不丢失

motif 仅用于 grouping。

完整 `NormalizedTrajectory.workflow`、source run/task/trace hash 仍保留在 trajectory/report provenance 中，用于审计，不作为 recovery grouping key。

### 5. 不做语义相似度猜测

v2 仍然 deterministic。

不会使用 embedding、LLM similarity、自然语言任务名或文件名把 pattern 强行合并。

只有 typed failure category、typed recovery strategy、紧邻 semantic action 完全一致时才聚合。

## CandidateGenerator

实现位置：

`experience/candidate.py`

Recovery candidate 不再只生成泛化 workflow。

现在标准 `SKILL.md` 明确包含：

- observed failure category
- observed recovery strategy
- source evidence count
- “不要原样重试失败动作”的恢复约束
- strategy-specific recovery guidance
- motif 中记录的 next semantic action

例如：

```text
# Recovery Motif

Observed failure category: test_failure
Observed recovery strategy: inspect

Recovery:
1. Do not retry the failed action unchanged.
2. Inspect the failure evidence...
3. Inspect the smallest relevant repository evidence...
```

Candidate 仍通过现有 `SkillCandidate.build` 生成，CandidateStore/PromotionGate/PromotionManager contract 不变。

## 测试

更新：

`tests/test_skill_evolution.py`

新增/调整覆盖：

- fixture recovery trace 产生 bounded recovery motifs；
- 同一次 failure 后多个 recovery escalation 都保留；
- 两条 full workflow 不同但 recovery motif 相同的 trajectory 聚合为 evidence_count=2；
- 同一 trace 重复同一 motif 仍只算 evidence_count=1；
- recovery candidate 明确包含 failure/strategy/next action。

更新：

`tests/test_real_skill_evolution_driver.py`

新增 end-to-end driver test：

- 构造两条完整 workflow 不同的 accepted traces；
- 两条都包含 `test_failure → inspect → INSPECT`；
- driver 通过 `batch_summary.json` 读取；
- 最终该 motif evidence_count=2；
- candidate `promotion_evidence_ready=true`；
- provider_calls 仍为 0。

## 验证限制

本次实现没有调用任何真实 LLM Provider。

尝试在执行环境 clone 当前 GitHub `dev` 后运行专项 pytest，但环境仍无法解析 `github.com`：

```text
Could not resolve host: github.com
```

因此完整 repository pytest 需要用户本地完成。

## 下一步

用户本地：

1. pull 最新 `dev`
2. 运行：
   ```bash
   python -m pytest -q \
     tests/test_skill_evolution.py \
     tests/test_real_skill_evolution_driver.py
   ```
3. 重新对同一真实 batch summary 执行：
   ```bash
   python scripts/run_skill_evolution.py \
     --repo /mnt/e/2806/forgeAgent/pr-test-agent-practice \
     --batch-summary /mnt/e/2806/forgeAgent/forge-agent/evals/results/pr-test-practice-batch-20260921-082816/batch_summary.json \
     --mine-only \
     --no-store
   ```
4. 检查真实 output 中哪些 motif 达到 evidence_count >= 2。
5. 只有实际存在有意义的 evidence-ready motif 后，才进入 real-model candidate evaluation。
