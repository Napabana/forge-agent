# P2 Stage 1：Planning v2 / Recovery v2 本地回归（DONE）

日期：2026-09-20

状态：**DONE**

验证基线：`dev@ab0f1b26ff6c79c5c12f6418185607ab939a658c`

## 1. 验证结论

用户已在本地完成：

- 前述失败点复验
- 相关专项回归
- 全量 pytest

并明确确认：**全部通过**。

最终通过轮次没有提供具体 passed / skipped 数量、完整 stdout 或耗时，因此本日志不补造数字。

## 2. 本轮已验证范围

### Planning v2

已验证：

- runtime-owned step identity
- deterministic slug / collision / non-ASCII fallback / bounded length
- no-plan / has-plan dynamic control surface
- current step-id enum
- model 不再负责 step status/version bookkeeping
- revision identity 与 terminal progress carry-forward
- terminal duplicate update 为 idempotent no-op
- illegal terminal rollback 拒绝
- Eval 不重复统计 `state_changed=false` completion

### Recovery v2

已验证：

- recovery budget 按 failure category 分离
- 同类 failure bounded exhaustion
- global hard ceiling
- Recovery Trace 同时保留 category/global accounting
- Planning revision 与 Recovery runtime 关联不回归

### Semantic Progress

已验证：

- repository content change 是 progress
- 首次 Skill load 是 progress
- duplicate Skill load 不是 progress
- plan create/revise 是 progress
- Planning no-op 不是 progress
- 新 test evidence 是 progress
- 重复相同 test evidence 不重复推进
- ordinary read 不无限刷新 progress
- Skill / Planning control action 同样进入统一 NO_PROGRESS guard

### Repository / Completion Guard

全量回归曾暴露 non-Git workspace 的 runtime log pollution：

```text
EventLog append
→ <repo>/logs/*.jsonl changed
→ filesystem fingerprint changed
→ false semantic progress / false repository change
```

修复后已验证：

- non-Git `repository_content_fingerprint()` 忽略 runtime `logs/`
- 修改真实 repository file 仍改变 fingerprint
- Day2 no-edit reflection 恢复
- Runner `require_changes=True` 不会被 EventLog 自身满足
- incomplete run 不进入 hidden acceptance

### Provider-aware strict schema

已验证：

- strict capability 与 ModelCapabilities 解耦
- `auto/on/off` config parsing
- compatible gateway `auto` 保守关闭
- explicit `on` 开启 OpenAI Chat / Responses strict shape
- Anthropic 保持 native `input_schema`
- recursive required/additionalProperties conversion
- optional-null normalization
- malformed payload 仍由 Runtime validation 拒绝

### MCP regression

全量回归曾暴露 crash fixture 的 2 秒 startup/discovery budget 在大 suite 下超时。

修复保持 production MCP manager 不变，仅把 crash fixture startup budget 与正常 stdio fixture 对齐。修复后全量回归全部通过。

## 3. 当前状态

本轮 deterministic implementation + regression validation 至此完成：

```text
Planning v2                 DONE
Recovery v2                 DONE
Semantic Progress           DONE
Strict Tool Schema wiring   DONE
Control-action progress     DONE
Repository log pollution    DONE
Regression suite            PASS (exact count not recorded)
```

## 4. 尚未完成的验证

本日志只证明 deterministic regression 已通过。

尚未执行新的 real-model Planning/Recovery v2 E2E，因此当前不能宣称：

- success-rate 提升
- pass@1 提升
- malformed planning call 已在真实模型上归零
- token 减少
- step 减少
- latency 减少
- recovery effectiveness 提升

## 5. 下一步：Real-model E2E

继续使用：

```text
Napabana/pr-test
branch: forge-p2-skill-demo
```

必须使用新 clone 或彻底 reset 的工作目录，不复用之前被 Agent 修改过的 fixture。

建议：

```bash
git clone --branch forge-p2-skill-demo --single-branch \
  https://github.com/Napabana/pr-test.git pr-test-planning-recovery-v2
```

然后从 forge-agent 仓库执行：

```bash
python -m entry.cli run \
  --repo ../pr-test-planning-recovery-v2 \
  --task "Fix the normalize_label regression with the smallest correct change. Follow the repository-specific release verification contract. Do not create a Git commit." \
  --no-stream
```

重点检查 Trace：

1. `plan_create` payload 不要求 model-owned id/status；
2. Runtime 生成 step id，后续 `plan_step_update` 使用同一 identity；
3. 正常 provider control surface 不在 create 前暴露 step update/revise；
4. 重复 terminal update 若出现，为 `idempotent=true,state_changed=false`；
5. Skill load / plan create/revise / new test evidence 能正确重置 semantic progress；
6. duplicate Skill / Planning no-op / ordinary read 不会无限刷新；
7. Tool / Test / NO_PROGRESS failure category 独立 accounting；
8. Completion Guard 最终仍由 repository/test completion contract 决定；
9. 当前 compatible gateway 在 `strict_tool_schema:auto` 下不发送未验证 strict contract。

## 6. Before / After 口径

Stage 1 原始真实 E2E 已记录：

- status: SUCCESS
- steps: 18
- tokens: 122,597
- wall time: 98.6s

新的 Planning/Recovery v2 E2E 尚未执行。

后续只能按同一任务、同一 fixture、同一模型/协议口径做定性和小样本比较，不能从一次 run 推导总体 success-rate 或性能结论。
