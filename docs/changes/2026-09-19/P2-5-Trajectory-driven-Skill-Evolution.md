# P2-5 Trajectory-driven Skill Evolution

日期：2026-09-19  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**  
实现代码提交：`af363b2acb536e8d9b169a57bce826e7ef6c0412`

## 目标

本轮只实现 offline、eval-gated 的 Skill evolution：

```text
Trace / Eval artifact
        ↓
deterministic Experience Mining
        ↓
isolated Candidate Skill
        ↓
existing P2-0 Evaluation Harness
        ↓
deterministic Promotion Gate
        ↓
explicit project Skill promotion
        ↓
existing P2-3 SkillCatalog
```

不实现在线强化学习、模型参数训练、当前 run 自改 prompt、自动 promotion、Multi-Agent evolution、Skill marketplace、MCP marketplace 或 remote autonomous install。

## 实现事实

### Canonical trajectory / eligibility

- `experience/trajectory.py` 读取既有 Trace v2 JSONL；不重新定义 transcript schema。
- Trace load 有 16 MiB / 20k event bound、UTF-8/JSONL 校验与 sha256。
- `TrajectoryRef` 保存 run/task/trace hash/ref 与可选 Eval trial correlation，不复制 Trace 内容。
- positive mining 严格要求 `run_status=success + acceptance_status=passed`；若提供 TrialResult，还要求 trial success。
- canceled、infrastructure failure、incomplete/gave_up、acceptance 未请求/失败都不会生成 successful experience。

### Deterministic mining / candidate

- `ExperienceMiner` 只消费 typed Tool / Plan / Recovery events，归一成 INSPECT / EDIT / TEST / REPLAN 等稳定操作。
- successful workflow 与 recovery workflow 分开；recovery pattern 使用 `failure_classified / recovery_selected / plan_revised`，不从自然语言猜。
- `CandidateGenerator` 保留 generator boundary；首版实现为 `DeterministicCandidateGenerator`，deterministic tests 不依赖真实 Provider。
- Candidate 最终仍输出 P2-3 可读的标准 `SKILL.md`。

### CandidateStore / provenance

默认 project store：`<repo>/.forge-agent/experience/`。

- candidate 与 `<repo>/.agents/skills/` 物理隔离，普通 Agent 默认不可加载。
- candidate version、content hash、source run/trace/task、pattern、evaluation、promotion decision 独立保存。
- candidate version 不可静默覆盖；store 有 path traversal、project boundary、artifact size、corrupt artifact 检查。
- lifecycle state：`DRAFT / EVALUATING / REJECTED / APPROVED / PROMOTED`。
- offline audit 使用 bounded `evolution_events.jsonl`；source Trace 保持 immutable。

### Evaluation Harness 接线

- `experience/evaluation.py` 直接复用 P2-0 `EvaluationHarness`，没有第二套 Trial loop。
- baseline 与 candidate-enabled 使用独立 output dir/variant。
- candidate-enabled 通过临时 Skill root 走现有 P2-3 SkillCatalog/SkillRuntime；没有直接 prompt 注入。
- 独立 `evals/fixtures/skill_evolution/suite.json` 覆盖 target / should-trigger / should-not-trigger / non-regression。
- process evidence 继续复用现有 `skill_selection` grader。

### Promotion Gate / explicit promotion

`PromotionGate` deterministic 返回：

- `PASS`
- `REJECT`
- `INSUFFICIENT_EVIDENCE`
- `EVALUATION_FAILED`

检查：

- candidate id/version/hash 与 EvaluationRecord 一致；
- evaluation infrastructure 是否正常；
- minimum source evidence；
- target/non-regression outcome；
- should-trigger / should-not-trigger；
- required process signal；
- step overhead；
- token overhead。

`PromotionManager.promote()` 不会因 Gate PASS 自动运行。显式 promotion 还要求：

- decision 已持久化且确实为 PASS；
- evaluation record 与当前 candidate version/hash 一致；
- candidate state 为 APPROVED；
- project Skill target 无 symlink/collision；
- 手工 Skill 无 Forge provenance 时禁止覆盖；
- managed upgrade 必须匹配 parent Skill version/hash。

approved Skill snapshot 保留，可对 Forge-managed project Skill 显式 rollback。

## Trace / Context / MCP 边界

- 不创建 Trace v3。
- 不向旧 Trace append evolution lifecycle。
- Context Compaction / HistoryWindow 不参与 mining；Miner 只读落盘 artifact。
- MCP Tool 只作为普通 tool/capability event 进入 trajectory；不增加 MCP evolution/install/marketplace。

## Fixture / test

新增：

- `tests/test_skill_evolution.py`
- `evals/fixtures/skill_evolution/suite.json`
- successful trajectory ×2
- failure→recovery→success trajectory
- unrelated trajectory
- canceled trajectory
- infrastructure-failure trajectory

测试源码覆盖 eligibility、typed recovery、deterministic mining、candidate identity/provenance、store isolation/collision/corruption/bounds/path safety、P2-0 Harness candidate overlay、trigger/non-trigger、Gate 四状态、regression/overhead/stale hash、explicit promotion、manual Skill collision、managed upgrade/rollback、bounded audit 与 package discovery。

## 当前实际验证

ChatGPT 执行环境实际完成：

```text
python -m py_compile /tmp/p25/experience/*.py /tmp/p25/tests/test_skill_evolution.py
→ PASS

standalone core smoke:
2 verified trajectories
→ one deterministic pattern
→ candidate
→ EvaluationRecord
→ PromotionGate PASS
→ persisted decision
→ explicit project promotion
→ PASS
```

限制：当前执行容器无法解析 `github.com`，因此没有 clone 当前仓库，也没有运行仓库级 pytest。以上只属于 syntax/core contract smoke，不等价于 repository regression。

## Evidence 边界

当前只可写 Implementation Fact：

> 基于 Trace / Eval artifact 实现 trajectory-driven Skill candidate mining，通过 deterministic promotion gate 对 target/non-regression/trigger/overhead 做检查，候选经显式 promotion 后才进入现有 project SkillCatalog。

当前不能写：

- Agent 自主进化；
- Agent 自动学习越来越强；
- success rate/pass@1 提升；
- token/latency 提升；
- online RL / DPO / GRPO；
- production continuous self-improvement。

等待用户本地完成 P2-5 专项、P2-3/P2-2/P2-1/P2-0/Trace/Failure Harness 回归、Evidence Pack 与全量 pytest 后，才能把状态从 `IMPLEMENTED / LOCAL VALIDATION PENDING` 收口为 `DONE`。
