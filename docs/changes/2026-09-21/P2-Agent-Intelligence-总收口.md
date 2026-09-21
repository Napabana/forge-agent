# P2 Agent Intelligence 总收口

日期：2026-09-21  
阶段状态：**DONE / REAL E2E CLOSED**

## 1. 这一阶段解决什么

P2 的目标不是继续增加普通 Tool，而是在既有：

~~~text
ExecutionRunner
→ Agent
→ ToolExecutor
→ Trace / RunResult
→ Acceptance
~~~

主链上补齐 Coding Agent 的任务级智能与评测闭环。

最终结构：

~~~text
P2-0 Evaluation Harness
        ↓
P2-1 Structured Planning
        ↓
P2-2 Failure-aware Recovery
        ↓
P2-3 Agent Skills
        ↓
P2-4 MCP Capability Integration
        ↓
P2-5 Trajectory-driven Skill Evolution
~~~

六项能力没有引入第二套 Agent loop。Planning、Recovery、Skills、MCP 进入现有 runtime；Evaluation Harness 是独立外层；Trajectory Evolution 位于 post-run offline pipeline。

## 2. 最终架构

~~~text
                         Runtime
                           │
Task
  ↓
ExecutionRunner
  ↓
Agent.run()
  ├─ PlanningRuntime
  ├─ RecoveryRuntime
  ├─ SkillRuntime
  └─ ToolExecutor
       ├─ native tools
       └─ MCPToolAdapter
  ↓
Trace v2 / RunResult
  ↓
Independent Acceptance
                           │
                           └────────────────────────────┐
                                                        ▼
                                                Post-run Evolution
                                                        │
                                                 ExperienceMiner
                                                        ↓
                                                 Candidate Skill
                                                        ↓
                                            P2-0 baseline/candidate eval
                                                        ↓
                                                 PromotionGate
                                                        ↓
                                    PASS / REJECT / INSUFFICIENT /
                                           EVALUATION_FAILED
                                                        ↓
                                       explicit promote() only on PASS
~~~

核心边界：

- 模型不能绕过 ToolExecutor 直接获得副作用能力；
- Planning / Recovery 只是 runtime control，不替代最终 acceptance；
- Skill scripts 不自动执行；
- MCP Tool 仍走 Hook / Permission / Cancel / Trace；
- Evolution 不在当前 run 中自改 prompt 或 Skill；
- Candidate 与正式 SkillCatalog 默认隔离；
- PromotionGate PASS 也不会自动部署。

## 3. P2-0：Coding Agent Evaluation Harness

核心路径：

- evals/coding_agent/schema.py
- evals/coding_agent/runner.py
- evals/coding_agent/graders.py
- evals/coding_agent/report.py

解决的问题：

在 P2 之前，仓库已有 Repo Map benchmark、Failure Harness、Acceptance、Evidence Pack，但没有统一的任务级：

~~~text
Task
→ isolated Trial
→ real ExecutionRunner
→ deterministic grader
→ outcome + process metrics
→ report
~~~

P2-0 固化：

- EvalTask / Trial / Grader / TrialResult / EvalReport；
- clean fixture repository；
- deterministic command/file/repository/Trace/process grader；
- steps、tokens、tool/test、planning/recovery/Skill/MCP metrics；
- fake backend 与 real-model evidence 明确分层；
- 默认不调用 Provider，真实模型必须显式开启；
- 输出目录不覆盖历史 artifact。

后续 P2-1～P2-5 全部复用这一层做 A/B 或 process verification。

## 4. P2-1：Structured Planning

核心路径：

- agent/planning.py
- agent/core.py

实现：

- typed ExecutionPlan / PlanStep / PlanRevision；
- planning_mode=off|auto|always；
- plan 作为 bounded runtime state 注入每轮 system context；
- mutation 前 Planning gate；
- plan revision 与 lifecycle Trace；
- plan completion 不等于 task completion，最终仍依赖 repo/test/acceptance。

关键设计点：

Planning 不是“Prompt 里让模型先想一想”，而是可观察、可 gate、可 revision 的 runtime state。

## 5. P2-2：Failure-aware Recovery

核心路径：

- agent/recovery.py
- agent/core.py
- agent/prompt.py

实现：

- typed FailureContext / RecoveryDecision / RecoveryPolicy；
- failure category：test/tool/permission/loop/no-progress/completion/infrastructure 等；
- strategy：retry / inspect / rerun_test / change_approach / replan / give_up；
- bounded attempts；
- structured replan gate；
- failure_classified / recovery_selected / recovery_exhausted / recovery_blocked Trace。

关键边界：

- Provider transient retry 仍由 Provider retry 层负责；
- infrastructure failure 不伪装成普通 recovery；
- Recovery 不允许无限 retry；
- 已有 plan 被证据推翻时，必须 revision 后才能继续 mutation。

## 6. P2-3：Agent Skills

核心路径：

- skills/catalog.py
- skills/runtime.py

Skill 路径：

~~~text
project: <repo>/.agents/skills/<name>/SKILL.md
global : ~/.forge-agent/skills/<name>/SKILL.md
~~~

实现：

- project/global catalog；
- frontmatter validation；
- project 同名覆盖 global；
- progressive disclosure；
- metadata 常驻；
- skill_load 加载完整 instructions；
- skill_reference_load 按需加载 reference；
- scripts 只暴露 manifest，不自动执行。

真实 P2-5 E2E 进一步暴露了 progressive-disclosure 的一个边界：如果 metadata description 本身包含完整操作结论，模型可能无需 skill_load 就拿到核心经验。

因此 future recovery Candidate renderer 已升级为：

~~~text
progressive_disclosure_v2
~~~

metadata 只描述 trigger，具体 next semantic action 保留在完整 Skill instructions。

## 7. P2-4：MCP Capability Integration

核心路径：

- mcp_integration/manager.py
- mcp_integration/adapter.py
- mcp_integration/registry.py

实现：

- official MCP Python SDK v2；
- stdio 与 Streamable HTTP；
- persistent client lifecycle；
- remote tool → Forge Tool adapter；
- mcp__<server>__<tool> namespace；
- schema / description / tool-count bounded；
- remote errors 映射到现有 ToolResult；
- ToolAnnotations 默认不可信；
- MCP Tool 继续走 ToolExecutor / Hook / Permission / Planning gate / Trace。

真实模型验收：

独立 pr-test:forge-p2-mcp-demo 上完成 MCP E2E。噪声治理后的 r3：

~~~text
SUCCESS
11 steps
73,197 tokens
75.1s
post-edit full tests: 16 passed
~~~

同 fixture 上上一轮成功 run 为：

~~~text
29 steps
295,016 tokens
779.2s
~~~

这只能表述为**同一 fixture 的单次 before/after 轨迹差异**。上一轮含 provider timeout，样本数也只有单次，因此不能写成稳定 token/latency improvement。

可直接从轨迹归因的改进是：

- file_edit 替代重复 file_write；
- 去掉 CRLF/EOF-byte 无效探索；
- MCP guidance 只调用一次；
- post-edit full test 后及时 FINISH；
- 无 permission/no-progress/replan 噪声链。

## 8. P2-5：Trajectory-driven Skill Evolution

核心路径：

- experience/trajectory.py
- experience/candidate.py
- experience/evaluation.py
- experience/promotion.py
- experience/store.py
- scripts/run_skill_evolution.py
- scripts/run_real_skill_evolution_eval.py

### 8.1 输入资格

只有：

~~~text
RunStatus.SUCCESS
+
independent acceptance=passed
~~~

的 trajectory 才能进入 positive mining。

cancel / incomplete / gave_up / infrastructure failure / acceptance 未验证，不会被当成“成功经验”。

### 8.2 真实 Trace 产生

独立 pr-test practice batch 最终得到 3 条 accepted + eligible Trace：

~~~text
Task A / B / C
84 accepted steps
1,227,602 tokens
1439.1s
3 eligible Trace v2
~~~

这些数字是 source-data execution facts，不是性能指标。

### 8.3 从 full workflow 到 recovery motif

首轮真实 mining 暴露旧 grouping 过严：

~~~text
整条 recovery workflow 精确匹配
→ 3 条真实任务全部被拆开
→ evidence_count 全为 1
~~~

因此升级为：

~~~text
recovery_motif_v2

failure category
→ recovery strategy
→ first semantic action
~~~

规则：

- successful workflow 仍完整精确 grouping；
- recovery 使用 bounded local motif；
- 同一 trace 对同一 motif 最多贡献一份 evidence；
- recovery escalation 可产生不同 motif；
- 不用 embedding / LLM similarity / task name 做语义合并；
- 完整 workflow 与 trace hash 继续保留 provenance。

同一 3 条真实 Trace 重新 mining：

~~~text
7 recovery motifs
3 motifs evidence_count=2
default min_evidence_count=2
~~~

### 8.4 Candidate 选择

三个 gate-ready motif 中：

~~~text
tool_failure → inspect → INSPECT
~~~

基本复述既有 RecoveryPolicy。

~~~text
no_progress → change_approach → PLAN
~~~

与 Planning + Recovery runtime 高度重合。

因此只选择：

~~~text
no_progress
→ change_approach
→ INSPECT
~~~

进入 real-model final gate。

这里没有为了得到 PASS 去降低 evidence threshold。

### 8.5 Real-model final gate

final gate 保留四角色：

~~~text
TARGET
SHOULD_TRIGGER
SHOULD_NOT_TRIGGER
NON_REGRESSION
~~~

baseline / candidate 各跑一次：

~~~text
4 roles × 2 variants = 8 real-model Agent trials
~~~

首次报告曾把 required process grader 混入 task outcome，导致 process failure 被重复描述成功能 regression。随后修正：

~~~text
Outcome:
run status
+ acceptance
+ required functional graders

Process:
skill selection
+ recovery motif ordering
~~~

并增加 --replay-existing，直接重分析原 8 个 trial artifact，严格 0 API。

最终 replay 结果：

- 四个 baseline/candidate 功能 outcome 全部成功；
- target / should-trigger 各有 2 次 failure classification + 2 次 recovery selection；
- Candidate metadata 均被 discover；
- skill_selected_count=0；
- skill_loaded_count=0；
- should-not-trigger / non-regression 没有误加载；
- 所有 case evaluation_failed=false；
- PromotionGate = REJECT；
- 没有 promotion。

因此最终结论：

> P2-5 的 trajectory → Candidate → real-model evaluation → deterministic PromotionGate 闭环真实跑通；该 Candidate 没有证明可验证增量价值，被 Gate 正确拒绝。

这比“为了展示自进化而强行 promotion”更符合当前证据。

## 9. P2-5 安全执行入口

### Offline mining

~~~bash
python scripts/run_skill_evolution.py \
  --repo /path/to/target-repo \
  --batch-summary /path/to/batch_summary.json \
  --mine-only \
  --no-store
~~~

这一步 0 Provider calls。

### Final-gate dry-run

~~~bash
python scripts/run_real_skill_evolution_eval.py \
  --mining-report /path/to/mining_report.json \
  --pattern-id pattern-... \
  --output-dir /path/to/output
~~~

默认 0 Provider calls。

### Real-model evaluation

只有显式 --execute 才调用 Provider。

### Offline replay

已有 real-model artifacts 时使用 --replay-existing。

它只读 baseline/candidate raw trial artifacts，0 Provider calls，不覆盖原始证据。

## 10. 测试与证据状态

P2-0～P2-5 均有 deterministic tests；用户已经在本地完成对应专项、兼容回归与最终 P2-5 收口回归，并确认全部通过。

最后一次 P2-5 收口后的回归，用户只提供“全过”的确认，没有提供最终 passed 数量和耗时，因此文档不补造具体数字。

真实模型证据目前主要有：

1. MCP 独立真实 E2E；
2. P2-5 practice batch 的 3 条 accepted source trajectories；
3. P2-5 Candidate 的 8 个 real-model final-gate trials。

不把 deterministic fixture 当成 real-model capability，也不把单次真实 run 推广为稳定 benchmark。

## 11. 最终可以声称什么

可以：

- 实现统一 Coding Agent Evaluation Harness；
- 实现 typed Structured Planning 与 Failure-aware Recovery；
- 实现 filesystem Agent Skills + progressive disclosure；
- 使用 official MCP SDK 将远端 capability 接入现有 Tool lifecycle；
- 基于 accepted Trace v2 做 deterministic recovery motif mining；
- 生成 Candidate Skill，并通过 baseline/candidate Evaluation Harness + PromotionGate 做受控晋升判断；
- 真实 final gate 中 Candidate 被 REJECT，证明拒绝路径与证据门禁工作；
- Promotion 需要显式动作，不允许 runtime 自动自改生效。

不能：

- “实现了在线 RL / 模型自训练”；
- “Agent 会自动越跑越聪明”；
- “P2-5 已证明 Skill 能稳定提升成功率”；
- 把单次 MCP before/after 写成稳定 75% token 优化；
- 把 fixture/deterministic regression 写成真实模型能力提升；
- 把被 REJECT 的 Candidate 描述成已经部署的自进化 Skill。

## 12. 阶段结论

P2 Agent Intelligence 到这里完成的是一条工程化闭环：

~~~text
可执行
→ 可观察
→ 可恢复
→ 可扩展 capability
→ 可评测
→ 可从历史轨迹提出 Candidate
→ 可拒绝不合格 Candidate
→ 只有经过 Gate 才允许显式部署
~~~

下一阶段不再继续堆 P2 功能。后续工作应转向：

- README / USAGE 与 Evidence 文档维护；
- 简历与面试表达；
- 更长期、更多任务的真实 benchmark；
- 只有出现新的明确工程问题时再扩展 Agent capability。
