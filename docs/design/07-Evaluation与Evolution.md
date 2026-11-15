# 07 Evaluation Harness 与 Trajectory-driven Evolution

> 基线：`dev@fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb`，2026-11-12

## 1. 功能目标

Evaluation 把 task、环境、grader、trial、artifact 和报告固定成可比较协议；Evolution 在运行后从
已验收 Trace 提取候选 Skill，并以独立评测和确定性 Gate 决定是否允许显式晋升。

## 2. 职责与边界

Harness 复用生产 `ExecutionRunner → Agent → ToolExecutor`，不实现第二套 Agent。grader 不进入
prompt。Evolution 不进入当前 run，不训练模型，不自动修改正式 Skills；Trace 是过程事实，
TrialResult/GraderResult 是评测判断。

2026-11-15 的生产 Trial schema v2 将自然 `run_status`、独立 `verifier_status`、
`failure_domain` 和 `scoreable` 分开。`success` 保留自然完成及验收语义；
`scoreable_success` 表示冻结策略下的最终快照评分。INCOMPLETE 的快照可以通过独立评分，
但不会改写为 Agent SUCCESS。策略在执行前记录 ID 与指纹，默认允许 deadline 内的
incomplete/gave-up/timeout 快照；Provider、Harness、环境、取消和未知故障不进模型能力分母。

模型调用证据来自受关联校验的 Trace，不能仅凭配置的 real-model 标签证明已执行模型。
`usage_complete` 为 false/null 时 token 均值不把未知当零。完整原始 Trial 仍保留，报告分别列出
总数、可评分数、不可评分原因、原始 Verifier PASS 和自然完成数。旧 schema v1 聚合仍可读取，
混合报告放进 `legacy_v1` 独立字段，不与 v2 分母混合。

materialize、factory、runner、grader、snapshot、metrics 和 cleanup 异常按阶段保存。
cleanup 在快照保存后执行，其异常不会覆盖原始 Agent/Verifier 结果。命令 grader 的进程启动
或超时故障记录为 Verifier ERROR，普通测试非零退出仍为 Verifier FAIL。Harbor 外部超时的
离线审计只有显式提供冻结策略、可信故障归属和 deadline 快照事实后才生成评分；否则保留 null。

## 3. 评测与演化链

```mermaid
flowchart LR
  SUITE[EvaluationSuite] --> MAT[materialize clean task repo]
  MAT --> RUN[EvaluationHarness -> ExecutionRunner]
  RUN --> TRACE[Trace v2 + patch + final state]
  TRACE --> GRADE[required/non-required graders]
  GRADE --> TRIAL[TrialResult + process/outcome metrics]
  TRIAL --> REPORT[aggregate report]
  TRACE --> LOAD[load_trajectory]
  TRIAL --> LOAD
  LOAD --> MINE[ExperienceMiner]
  MINE --> GEN[DeterministicCandidateGenerator]
  GEN --> STORE[CandidateStore isolated]
  STORE --> PAIR[baseline/candidate EvaluationHarness]
  PAIR --> GATE[PromotionGate]
  GATE -->|PASS + explicit action| PROMOTE[PromotionManager -> project Skill]
  GATE -->|REJECT/insufficient/error| KEEP[not promoted]
```

## 4. 关键执行流程

Suite 可基于内嵌 files 或冻结 external repository commit。materialization checkout exact commit，
移除原 Git history、应用 setup overlay，再初始化单一 trial baseline，避免答案从历史泄漏。
reference solution 在真实模型前由 deterministic grader 自检。每个 trial 独立目录，输出拒绝覆盖。

Eval 与 CLI run 共享 `entry/context_common.py::configured_agent_options` 和
`build_context_policy`，使用相同的 Repo Map 模式/预算、TokenCounter 元数据、测试 Python 解释器和
可选累计调用/Token 限额。Eval 每个 Trial 创建独立 Context Policy；suite 显式覆盖步骤数/窗口预算
仍作为实验条件记录。普通 Eval 工具注册复用 CLI run 的 `_build_run_registry`，Agent 不持有 git commit
工具；Grader/Acceptance 依旧是宿主控制的独立阶段。

Run metadata 显式记录 compaction 参数、TokenCounter 名称、测试解释器、工具清单、无确认回调的安全
默认权限配置、请求窗口和累计限额。Planning/Recovery/Skills/MCP 仍由 architecture variant 明确覆盖；
这些合法差异作为 A/B 条件保留，不要求所有入口采用相同交互权限或同一工具集。

Harness 构造 variant-specific AgentConfig/Skills/MCP，调用 Runner，随后运行 command/file/
repository_state/run_trace/test-change/recovery-motif 等 grader，保存 patch/final state/trace refs。
required outcome grader 与 Run/Acceptance 决定 success；process grader 可独立记录。

Evolution 只接受 `RunStatus.SUCCESS + acceptance=passed` 的 positive trajectory。successful workflow
按完整 typed workflow；recovery 使用 `failure category → recovery strategy → first semantic action`
有界 motif，同一 trace 对同一 motif 最多一份 evidence。Candidate 存于
`.forge-agent/experience`，普通 Catalog 看不到。PromotionGate 检查 evidence、outcome、
non-regression、process/trigger、overhead、hash/version；promotion 必须显式调用。

## 5. 核心数据结构

- `EvaluationSuite/EvalTask/RepositorySource/TrialConfig`：冻结协议。
- `GraderSpec/GraderResult/TrialMetrics/TrialResult/EvalReport`：判定与产物。
- `TrajectoryRef/NormalizedTrajectory/ExperiencePattern`：来源与规范化模式。
- `SkillCandidate/EvaluationRecord/PromotionDecision`：候选、配对证据、Gate 结果。
- `CandidateStore`：原子 artifact、status、evaluation、decision、approved snapshot。

## 6. 源码对应关系

| 符号 | 职责 |
|---|---|
| `materialize_task_repo/validate_reference_solution` | 冻结环境与 reference 自检 |
| `EvaluationHarness.run/_run_trial` | trial 编排与生产 Runner 接线 |
| `grade/required_graders_passed/extract_metrics` | outcome/process grader |
| `load_trajectory/ExperienceMiner.mine` | Trace 规范化与 motif grouping |
| `DeterministicCandidateGenerator.generate` | 标准 SKILL.md candidate |
| `evaluate_candidate/build_evaluation_record` | baseline/candidate 对照 |
| `PromotionGate.evaluate` | 四状态确定性决策 |
| `PromotionManager.promote/rollback` | 显式 project Skill 变更 |

## 7. 关键实现细节

- 默认 CLI 不带 real-model flag 只做 validation，并写 `not_executed`，不会构造 Provider。
- output 与 process 分离：required process grader 不重复污染功能 outcome。
- Eval artifact 保存 source/suite/model/budget/Repo Map/MCP identity，report 比较时拒绝 drift。
- Candidate identity/hash 排除本机 artifact path，source evidence 去重；旧 Candidate 不因 renderer
  变化冒充新版本。
- Promotion 区分 PASS、REJECT、INSUFFICIENT_EVIDENCE、EVALUATION_FAILED；基础设施失败不算候选失败。
- manual Skill 无 Forge provenance 时禁止覆盖；managed upgrade/rollback 校验 parent version/hash。

## 8. 设计取舍与真实局限

Benchmark V1 R2：同一冻结 12-task、48 real-model trials，baseline `23/24`，Full P2 `23/24`，
delta 0.0 pp；Full P2 成功 trial mean steps/tokens/time 均更高。它说明当前组合策略未在该 suite
提高 observed acceptance，不代表所有任务或模型。

P2-5：3 条 accepted source traces 产生 recovery motifs，选中一个 Candidate 做 8 个 real-model
final-gate trials；replay 后功能 outcome 成功，但 Candidate 未 select/load，process 不通过，
PromotionGate REJECT，未晋升。这证明 Gate 会拒绝缺乏增量证据的候选，不证明“自动进化”。

## 9. 测试与验证

`tests/test_coding_agent_eval.py`、`tests/test_coding_agent_benchmark_v1.py`、
`tests/test_abstention_scoring_v2.py`、`tests/test_coding_agent_abstention_pilot.py`；
`tests/test_skill_evolution.py`、`tests/test_real_skill_evolution_driver.py`、
`tests/test_real_skill_evolution_eval.py`。真实数字只引用冻结 report/evidence，不重新运行收费评测。

## 10. 面试理解重点

**如何避免 Agent 控制测试结果？** fixture/grader/Acceptance 由 Harness/Runner 持有，不进入 prompt，
并保留独立 artifact。
**为什么 process 与 outcome 分开？** 任务正确不代表候选机制被采用；合并会把 adoption failure
重复算成功能 regression。
**为什么 Evolution 不是在线自学习？** 它是 post-run、离线、deterministic mining + eval gate，
最终 promotion 仍需显式动作。
