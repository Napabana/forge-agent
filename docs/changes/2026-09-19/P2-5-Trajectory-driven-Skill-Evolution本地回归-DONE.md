# P2-5 Trajectory-driven Skill Evolution 本地回归 — DONE

日期：2026-09-19  
状态：**DONE**  
已验证实现 HEAD：`ac5f30d4951cef8d2840fdbd8bbebe71352f1e48`

## 验证结论

用户已在本地完成并明确确认以下验证全部通过：

- P2-5 `tests/test_skill_evolution.py` 专项；
- P2-3 Agent Skills 回归；
- P2-2 Failure-aware Recovery 回归；
- P2-1 Structured Planning 回归；
- P2-0 Coding Agent Evaluation Harness 回归；
- Trace v2 / Runner 回归；
- Failure Harness / isolate 回归；
- Evidence Pack 只读校验；
- 全量 pytest。

最终通过轮次未提供具体 passed 数量、完整 stdout 或耗时，因此本日志不补造数字。

## 已锁定的 deterministic evidence

### Trajectory / Mining

- 只有 success + independent acceptance pass 进入 positive mining；
- canceled / infrastructure failure / acceptance 未验证不会生成 successful experience；
- typed `failure_classified / recovery_selected / plan_revised` 可形成 recovery pattern；
- unrelated workflow 不会被合并；
- mining 对 input order deterministic；
- stable identity 不依赖本机 artifact path；
- 同一 source run/trace 不重复计 evidence。

### Candidate / Store

- Candidate 使用标准 P2-3 `SKILL.md` 格式；
- candidate store 与正式 `.agents/skills/` 物理隔离；
- candidate id/version/content hash/provenance 可验证；
- duplicate/corrupt/oversized/path traversal/repository-boundary 有 deterministic rejection；
- candidate/evaluation/decision artifact 与 lifecycle state 分离。

### Evaluation / Gate

- candidate evaluation 复用现有 P2-0 `EvaluationHarness`；
- baseline 与 candidate-enabled 分开输出；
- target / should-trigger / should-not-trigger / non-regression process contract 可验证；
- EvaluationRecord 保存 candidate hash/version 与 baseline/candidate trial/trace correlation；
- PromotionGate 区分：
  - `PASS`
  - `REJECT`
  - `INSUFFICIENT_EVIDENCE`
  - `EVALUATION_FAILED`
- required outcome、non-regression、trigger、process、step/token overhead 与 stale candidate hash/version 均进入 deterministic gate。

### Promotion / Rollback

- Gate PASS 不自动部署；
- promotion 必须验证 persisted PASS decision + matching EvaluationRecord + APPROVED candidate state；
- project Skill target 有 repository-boundary / symlink 检查；
- 无 Forge evolution provenance 的用户手工 Skill 不会被静默覆盖；
- Forge-managed Skill upgrade 使用 parent version/hash；
- approved snapshot 支持显式 rollback；
- promotion 后仍由现有 P2-3 `SkillCatalog` 发现和运行。

### Evidence / Packaging

- `experience*` 已进入 setuptools package discovery 与 coverage source；
- offline evolution lifecycle 使用 bounded metadata audit，不回写 source Trace；
- P2-0 frozen suite 和历史 frozen eval result 未为 P2-5 改写。

## Evidence 边界

本轮可以说：

> 实现 trajectory-driven、eval-gated 的 Skill evolution pipeline：从已验收 Trace/Eval artifact 做 deterministic experience mining，生成隔离 Candidate Skill，复用现有 Evaluation Harness 做 target/non-regression/trigger/overhead 检查，并在 deterministic promotion gate 通过后显式进入 project SkillCatalog。

本轮不能说：

- Agent 会自动持续变聪明；
- 已证明 real-model success rate / pass@1 提升；
- 已证明 trigger accuracy、token、step、latency 改善；
- 在线 RL / DPO / GRPO；
- 自动 promotion；
- production continuous self-improvement。

## P2 阶段状态

P2 Agent Intelligence：

- P2-0 Evaluation Harness — DONE
- P2-1 Structured Planning — DONE
- P2-2 Failure-aware Recovery — DONE
- P2-3 Agent Skills — DONE
- P2-4 MCP Client / Tool Adapter — DONE
- P2-5 Trajectory-driven Skill Evolution — DONE

P2-0 ～ P2-5 至此全部完成。
