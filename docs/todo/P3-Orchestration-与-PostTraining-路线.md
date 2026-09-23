# P3：Orchestration Optimization 与 Small-model Post-training 路线

日期：2026-09-23
状态：PLANNED / IMPLEMENTATION DEFERRED
事实基线：dev@03eb439f094f60e686a9738c0f4bd4f306f42fd3

## 0. 为什么进入 P3

P2 Agent Intelligence 已完成 Structured Planning、Failure-aware Recovery / Replanning、Agent Skills、MCP capability integration、Coding Agent Evaluation Harness 与 Trajectory-driven Skill Evolution。

Benchmark V1 R2 已完成冻结 real-repository A/B：

~~~text
source:
Napabana/pr-test@23019998f2e801e79dea59fd23fc49c58fc20038

12 tasks × 2 repetitions × 2 variants
= 48 real-model trials

baseline_react:
23 / 24 = 95.8%

planning_recovery_skills:
23 / 24 = 95.8%

absolute delta:
0.0 pp
~~~

Full P2 在 successful trials 上相对 Baseline：

~~~text
mean steps:
12.30 → 15.65
≈ +27.2%

mean total tokens:
169,602 → 223,881
≈ +32.0%

mean wall time:
102.02s → 118.32s
≈ +16.0%
~~~

Trace 同时暴露：

~~~text
Planning:
24 / 24 创建 plan
plan revisions = 0

Recovery:
recovery_selected = 20
其中 recovery-tagged trials = 6
非 recovery-tagged trials = 14

Skills:
should-trigger trials = 20
selected / loaded = 1 / 20
process match = 0 / 20
~~~

因此 P3 不继续横向堆新 Agent capability，而是回答两个问题：

1. 不训练模型，仅优化 Forge orchestration，是否能减少无效开销并提高 capability utilization？
2. 若使用较弱的小模型，Planning / Recovery / Skills scaffold 是否会产生更明显收益，并能否通过 SFT / Preference Optimization / Agent RL 进一步学习 Agent policy？

## 1. 当前判断：V1 同时存在 ceiling effect 与 orchestration 问题

### 1.1 Ceiling effect

deepseek-v4.1-flash 的 Baseline 已达到 23 / 24 = 95.8%，当前 12-task suite 对强模型只剩 1 个 trial 的 improvement headroom。

因此：

~~~text
Full P2 没有提升
≠
Planning / Skills 在更难任务或更弱模型上没有价值
~~~

V1 适合证明 real-repository Harness 可稳定运行、capability runtime 确实进入 production Agent path、当前 Full P2 没有在该 suite 上提高 acceptance，以及 Trace 能暴露 orchestration 行为。

V1 不适合继续作为强模型能力提升 benchmark。

### 1.2 Orchestration 本身仍有问题

不能把全部结果解释为“任务太简单”。

#### Planning

核心文件：agent/planning.py。

decide_planning() 已支持 off / auto / always。V1 Full P2 使用 always，因此所有任务都必须建立 plan。

虽然 auto 已存在，但当前规则主要依赖：

- require_tests；
- prompt 中多文件 path hints；
- 明显 multi-step language。

这仍属于轻量静态 heuristic，没有利用：

- Repo Map size / affected-symbol count；
- task ambiguity；
- mutation scope；
- initial exploration evidence；
- previous failed attempt；
- expected verification complexity。

V1 的 24/24 create、0 revision 表明 Structured Planning runtime 已经工作，但 always-on policy 让 Planning 更像固定前置协议，而不是只在需要时启用的 adaptive scaffold。

#### Recovery

核心文件：agent/recovery.py。

RecoveryPolicy 已能区分 TEST_FAILURE / TOOL_FAILURE / LOOP / NO_PROGRESS / PERMISSION 等 category，并选择 INSPECT / CHANGE_APPROACH / REPLAN / GIVE_UP 等策略。

但 V1 中 20 次 recovery selection 有 14 次出现在非 recovery-tagged trials，说明 trigger/classification boundary 偏宽。

需要区分：

~~~text
正常探索中的预期失败
≠
需要策略恢复的失败
~~~

例如第一次 pytest 暴露预期 bug、probing command 返回非零、repository navigation 中临时路径判断错误，不一定都应该消耗 structured recovery budget。

#### Skills

核心文件：skills/runtime.py、skills/catalog.py。

当前 progressive disclosure 路径是：

~~~text
catalog metadata
    ↓
模型主动 skill_load
    ↓
完整 Skill instructions
    ↓
可选 skill_reference_load
~~~

Skill selection 基本依赖模型自己看到 catalog 后主动调用 skill_load。

V1：

~~~text
should-trigger = 20
loaded = 1
process match = 0
~~~

因此当前已有 Skill Runtime，但尚没有成熟的 Skill Selection / Routing Policy。

这是 P3 中最明确的工程改进点之一。

## 2. Track A：不训练模型的 Orchestration Optimization

该 Track 不新增第二套 Agent loop，也不增加 Multi-Agent。

目标是：

~~~text
same model
same tools
same tasks
same acceptance
↓
更准确地决定
什么时候 Plan
什么时候 Recovery
什么时候 Load Skill
~~~

### P3-A1 Planning Gating

目标：always-on planning → adaptive planning。

建议基于已有 planning_mode=auto 扩展 decision signals：

- task 是否明确单文件 / 多文件；
- Repo Map 中候选 symbol / path 数量；
- require_tests 是否只是简单 regression；
- mutation scope；
- configuration + service + API 多层联动；
- task 是否包含 ambiguity / compatibility requirement；
- initial read-only exploration 后是否仍存在多个 plausible approaches。

首版仍保持 deterministic gate，避免增加额外 Planner LLM call。

建议记录：

- planning_enabled reason；
- planning_skipped reason；
- plan creation overhead；
- simple-task false-positive planning rate；
- complex-task planning coverage。

目标不是让 Planning 次数越多越好，而是简单任务不付 Planning 成本，复杂任务才使用结构化 plan。

### P3-A2 Recovery Trigger Precision

目标：failure observed 不等于立即 structured recovery。

增加 failure eligibility gate，可将 failure 分成：

~~~text
EXPECTED_EVIDENCE
RECOVERABLE_FAILURE
STRATEGY_FAILURE
INFRASTRUCTURE
~~~

第一次目标测试失败，如果本来就是为了复现 bug，则属于 expected evidence，不应消耗 recovery budget。

更适合真正 Recovery 的情况：

- 相同 failure 重复出现；
- edit 后测试仍以同一 root-cause failure 失败；
- no semantic progress；
- permission/capability blocked；
- loop detector 命中；
- completion rejected；
- 当前 plan 被新证据推翻。

目标指标：

- recovery precision；
- non-recovery-task trigger rate；
- repeated-failure recovery rate；
- recovery → actual strategy change rate；
- replan utilization。

### P3-A3 Skill Selection / Routing

当前 Skill runtime 不需要推翻，重点增加 Selection Policy。

候选设计按复杂度由低到高：

A. Metadata lexical routing

根据 task description、Repo Map / file type、test requirement、failure category 对 Skill metadata 做 deterministic / lexical rank，给模型展示 Top-K，而不是全 catalog。

B. Rule-based auto-suggest

只注入 Suggested Skills，由模型决定是否 skill_load。

C. Selector decision

增加一个轻量 selection policy，根据 task + current state 输出 none 或 skill name。首版优先 deterministic / cheap selector，不新增第二 Agent。

需要区分：

~~~text
discover
select
load
use
benefit
~~~

不能再把“Skill 文件存在”视为“Skill 被有效使用”。

目标指标：

- should-trigger selection recall；
- wrong-skill rate；
- should-not-trigger false-positive rate；
- load rate；
- loaded Skill 后的 steps / acceptance delta。

### P3-A4 Better Benchmark Tasks

V1 强模型 Baseline=95.8%，存在 ceiling effect。

后续 Benchmark V2 应增加更有 discrimination 的任务：

- 4～8 文件跨层修改；
- 任务只描述行为，不直接给出目标文件；
- repository navigation；
- backward compatibility；
- config + service + registry + tests 联动；
- 第一次合理方案会被隐藏约束否定；
- 需要根据新 evidence 修改执行路径；
- 有真正 plan revision / change-approach 场景。

目标不是故意让模型失败，而是将 strong-model Baseline 控制到有可比较空间的区间。理想观察区间约为 50%～80%，而不是接近 100%。

## 3. Track B：Small-model × Scaffold 实验

本 Track 暂不立即执行。

用户当前计划：

> 先回 MiniMind 学习 SFT、DPO 或 Agent RL，再决定是否进入 small-model post-training。

完成相关学习后，再启动 P3-B。

### P3-B0 Small-model untrained baseline

先选择一个开源 instruct/code model，并通过 OpenAI-compatible serving 接入现有：

~~~text
llm/router.py
→ llm/openai_compat.py
~~~

不需要把模型实现写进 Forge Agent。

实验矩阵：

| Model | Agent |
| --- | --- |
| strong hosted model | ReAct |
| strong hosted model | Full/Optimized Scaffold |
| small open model | ReAct |
| small open model | Full/Optimized Scaffold |

核心问题：

> Scaffold 的收益是否随 base-model capability 改变？

如果出现：

~~~text
strong model:
ReAct ≈ Scaffold

small model:
ReAct < Scaffold
~~~

则说明结构化 Agent runtime 对弱模型更有价值。

如果 small model 上 Scaffold 仍无收益，则优先继续检查 Forge orchestration，而不是马上训练。

## 4. Track C：Trajectory Post-training

只有 P3-B0 证明 small model 本身具备基本 tool calling / coding 能力后再进入。

### C1 Dataset split

必须严格分开：

~~~text
training tasks
validation tasks
held-out benchmark tasks
~~~

不能把最终 benchmark trajectory 用于训练后再报告同一 benchmark 提升。

### C2 SFT

Forge Trace v2 可转换为 Agent trajectory dataset：

~~~text
Task
+ Tool schemas
+ Repository context
+ Action
+ Observation
+ ...
+ Finish
~~~

SFT 的重点不是训练自由 CoT，而是训练 state → next Agent action，特别包括：

- 是否 Planning；
- plan step；
- skill selection/load；
- tool selection；
- failure 后 next action；
- test / verify；
- finish timing。

### C3 Preference Optimization / DPO

利用同一 state 下的好/坏 trajectory 或 action 构造 preference。

chosen 可以是：正确 Skill、有效 inspect、必要时 replan、更少无效步骤、最终 acceptance pass。

rejected 可以是：错误 Skill、重复 recovery、无意义 plan bookkeeping、重复无进展 Action、premature finish。

Preference 信号可来自：

- acceptance；
- test outcome；
- steps；
- token usage；
- wrong skill；
- recovery over-trigger；
- loop；
- completion rejection；
- plan revision quality。

### C4 Agent RL

在 SFT / preference training 后，再考虑 outcome-based Agent RL。

训练 reward 应优先基于 deterministic outcome：

~~~text
acceptance pass
tests pass
required behavior
~~~

效率可以作为次级 reward shaping signal，但不能为了少 steps 鼓励模型跳过验证。

## 5. 最终实验矩阵

如果进入 post-training，建议最终矩阵为：

| Model | Training | ReAct | Optimized Scaffold |
| --- | --- | ---: | ---: |
| Small | Base/Instruct | A | B |
| Small | SFT | C | D |
| Small | SFT + DPO | E | F |
| Small | SFT + RL（可选） | G | H |

这样可以分别回答：

~~~text
Scaffold effect:
B - A

SFT effect:
C - A / D - B

DPO effect:
E - C / F - D

RL effect:
G - E / H - F
~~~

以及更核心的问题：

> Agent intelligence 应该由 external scaffold 提供多少，又应该通过 post-training 内化多少？

## 6. 当前执行决策

### 现在不做

暂不立即：

- 购买/租用 GPU 训练；
- 下载小模型；
- 构造大规模 Agent dataset；
- 实现 SFT / DPO / RL pipeline；
- 修改正式 Benchmark V1 R2；
- 继续添加 Multi-Agent / parallel tool 等新 feature。

### 现在做

1. 保留 Benchmark V1 R2 为当前正式 evidence；
2. 用户先通过 MiniMind 学习 SFT / DPO / Agent RL；
3. Forge Agent 暂停功能扩张；
4. 优先完善简历与面试叙事；
5. 学习完成后再从 P3-B0 small-model baseline 重新决策：
   - 若 small-model scaffold 有明显收益 → 进入 SFT / DPO；
   - 若无明显收益 → 先做 P3-A orchestration optimization；
   - 若当前求职节奏更重要 → P3 保持 roadmap，不强行实现。

## 7. 简历/面试边界

当前已经可以准确表述：

> 构建 real-repository Coding Agent Evaluation Harness，在 48 个 real-model trials 上比较 Baseline ReAct 与 Planning + Recovery + Skills；两者 independent acceptance 均为 95.8%，但 Full P2 带来额外 steps/token/latency 开销。进一步通过 Trace 定位 always-on planning、recovery 过触发和 skill adoption 不足，并据此设计 adaptive orchestration 与 small-model post-training 路线。

不要表述：

- Planning / Recovery / Skills 已提升成功率；
- Agent 已通过 DPO/RL 自进化；
- 小模型训练已完成；
- Benchmark 证明总体 pass@1=95.8%。

这些目前都没有证据。
