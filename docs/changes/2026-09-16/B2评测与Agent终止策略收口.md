# 2026-09-16 B2 评测与 Agent 终止策略收口

## 1. 背景

本轮工作的起点是 Context Policy B2 真实 Coding Agent 消融评测。目标不是只验证离线历史压缩，而是在真实 `ExecutionRunner`、真实模型调用、真实工具执行和 hidden verifier 下比较：

- `baseline`
- `pruning_only`
- `hybrid_compaction`

B2 使用 3 个受控长历史 case：

1. `long-hard-constraint`：早期硬约束 + 长自然语言历史。
2. `huge-tool-history`：大量历史 Tool Observation。
3. `superseded-state`：旧失败状态已被新的 PASS 状态覆盖。

B2 v2 固定：

- `budget_tokens = 8000`
- `max_steps = 12`
- 严格 `pass_at_1 = agent success AND hidden verifier pass`
- 额外记录 `verifier_pass_rate` 和 `max_steps_exhausted_rate`

本轮结果文件位于：

- `evals/results/context_policy_agent_ablation_v2/report.json`
- `evals/results/context_policy_agent_ablation_v2/raw.jsonl`
- 对应 `traces/` 目录

---

## 2. B2 v2 结果如何解读

当前 3-case 小样本 aggregate：

| Variant | strict pass@1 | verifier pass | mean total tokens | mean latency |
|---|---:|---:|---:|---:|
| baseline | 0/3 | 2/3 | 123167 | 271.8s |
| pruning_only | 1/3 | 2/3 | 114117 | 264.9s |
| hybrid_compaction | 1/3 | 3/3 | 94444 | 87.9s |

这组数据不能直接拿严格 `pass@1` 当最终结论，因为 B2 暴露了两个与 Context Policy 本身无关的 Agent 生命周期问题：

1. completion guard 对 Agent 自己 `git commit` 的修改会产生假失败；
2. Agent 在代码已经正确、hidden verifier 已通过后，仍可能继续探索/验证直到撞上 `max_steps`。

因此当前 B2 v2 的正确结论是：

> Hybrid Context 在 3/3 case 上都生成了 hidden-verifier-passing 的最终代码状态，并且平均 token/latency 有正向信号；但严格任务成功率被 completion/termination 行为污染，不能作为最终性能结论。

这不是继续调 benchmark 参数的理由，而是发现了真实 Agent 工程缺陷。

---

## 3. 问题一：completion guard 对提交后的 clean repo 误判

### 3.1 现象

至少两个 B2 run 出现：

- hidden verifier = passed
- patch/最终文件内容正确
- Agent 最终 `FINISH`
- 但 Runner 返回：

```text
Task requires repository changes, but the repository state did not change.
```

Trace 显示模型在完成修改后主动执行了类似：

```bash
git add <file> && git commit -m "..."
```

旧 completion guard 的 repo state 只比较：

```text
git status + git diff HEAD
```

于是：

```text
初始：旧 HEAD + clean working tree
最终：新 HEAD + clean working tree
```

两边都表现为“clean”，从而误判“仓库没有变化”。

### 3.2 修复思想

仓库变化的事实不能只看 working tree；必须同时纳入 HEAD lineage。

仓库中已有统一实现：

- `context/repository_state.py::repository_fingerprint()`

其语义是：

```text
HEAD commit + working-tree snapshot
```

因此 completion guard 不再维护另一套 repo state 逻辑，而是直接复用该统一指纹。

### 3.3 已实现

修改：

- `agent/core.py`
  - import `repository_fingerprint`
  - `_get_repo_state()` 改为直接返回 `repository_fingerprint(repo_path)`

对应提交：

- `13cb0352111940107bf0a94bd0a14ada6ddb7513`
  - `fix: make completion guard commit-aware`

### 3.4 回归测试

在 `tests/test_agent_completion_guards.py` 新增真实 Git repo 回归：

```text
初始化 baseline commit
→ Agent file_write 修改文件
→ 工具内部 git add + git commit
→ Agent FINISH
→ 必须 RunStatus.SUCCESS
```

该测试覆盖的是本次 B2 中真实出现的失败路径，不是纯 mock 状态比较。

---

## 4. 问题二：max_steps 与 Agent 收尾行为

### 4.1 原始现象

`superseded-state` 中三个 variant 最终代码都通过 hidden verifier，但都出现过：

```text
Reached max_steps limit (12)
```

Hybrid 的 trace 甚至已经在最后阶段完成 compile/验证，但没有及时返回 final response。

初步做法是在最后 3 个 model step 给模型注入临时提示，要求减少无意义探索并优先完成必要修改/验证。

### 4.2 已实现的当前版本

修改：

- `agent/prompt.py`
  - 增加 `STEP_BUDGET_WARNING`
  - 增加 `step_budget_warning(remaining)`
- `agent/core.py`
  - 最后 3 个 model step 在 `_build_messages()` 之后临时 append warning
  - warning 只对当前 LLM request 可见，不写入 canonical `ConversationHistory`

对应提交：

- `ca916759d9fcadef9594467791d9bb92235db5a9`
  - `feat: add step budget finalization prompt`

回归测试：

- `tests/test_agent_completion_guards.py`
  - 验证 3/2/1 countdown 只出现在对应 request
  - 不会累计进入后续 canonical history

测试提交：

- `9f37acf062d64a3f2bb081e1e4cde87272586cb4`
  - `tests: cover committed changes and step budget hints`

### 4.3 重新审视后的设计结论

经过进一步讨论，不能把 `max_steps` 从“系统安全熔断器”变成“模型必须满足的任务 deadline”。

用户目标是：

> 让 Agent 尽可能真正解决问题，同时让最坏情况下的资源消耗有界。

而不是：

> 让 Agent 在固定 N 步内无论如何都输出完成。

如果频繁告诉模型：

```text
Step 10/12，只剩 3 步，赶紧结束
```

可能产生错误激励：

- 跳过必要分析；
- 跳过必要验证；
- 在任务未完成时为了 deadline 过早 `FINISH`；
- 将“按时结束”错误优化为高于“真正完成任务”。

因此下一阶段不应直接扩展为“每轮都注入 Step X/Y”。

---

## 5. max_steps 的正确定位

`max_steps` 应保留，但其定位是：

> 对 LLM → Tool → Observation 决策链提供最坏情况执行上界。

它解决的是资源失控，不负责证明任务可以完成。

### 5.1 为什么不能只靠 Loop Detector

Loop detector 擅长发现：

```text
AAA
ABABAB
ABCABCABC
```

等明显无进展循环。

但 Agent 可能执行：

```text
read A
read B
grep C
run test
read D
git status
read E
run another test
...
```

每一步都不同，未必形成可识别周期，但仍可能长期无效探索。

### 5.2 为什么不能只靠 Token Budget

不同 step 成本差异非常大：

- 一个 `git_status` 很便宜；
- 一个大仓库测试可能很慢；
- 一个大 context LLM request 可能很贵；
- 一个文件读取又很便宜。

因此 token budget 和 step upper bound 保护的是不同维度。

### 5.3 正常成功路径与资源耗尽路径必须分离

正确语义应是：

```text
Agent thinks task is done
        ↓
FINISH
        ↓
Completion Guard / Verification
        ↓
requirements satisfied
        ↓
SUCCESS
```

资源耗尽是另一条路径：

```text
仍未满足完成条件
        ↓
max_steps / timeout / token-cost budget exhausted
        ↓
MAX_STEPS / INCOMPLETE / TIMEOUT
```

绝不能因为资源快耗尽就把未完成任务标成 SUCCESS。

---

## 6. 对“步数提示”的下一版设计方向

### 6.1 当前 warning 暂时保留，但需要重审 wording

当前最后 3 步 warning 已经实现，但其中“stop broad exploration / return final summary immediately”存在 deadline pressure 风险。

下一窗口第一件事不是继续增加每轮 Step X/Y，而是重新设计该提示，使其核心语义变成：

```text
[RESOURCE BUDGET LOW]

Execution resources are running low.

- Do not claim completion unless completion requirements are actually satisfied.
- Prioritize the highest-value action toward completion.
- Avoid optional exploration or cleanup.
- If the task is already complete and sufficiently verified, return the final result.
- If it is not complete, continue working on the most important unresolved requirement.
```

重点：

> 低预算信号只能减少低价值动作，不能要求未完成任务强行结束。

### 6.2 不建议默认每轮暴露精确 Step X/Y

原因：

1. step 是粗糙资源单位；
2. 容易诱导模型针对 benchmark/上限优化，而不是针对任务完成优化；
3. 当前 B2 的 `max_steps=12` 本来就是评测成本控制，不代表生产 Agent 应该在 12 步内完成；
4. 生产默认 `AgentConfig.max_steps=40`，不应被 B2 的 12 步限制反向塑造 Agent 行为。

### 6.3 更长期的方向：资源预算而非步数预算

更合理的模型侧软信号是资源状态，例如：

```text
execution budget: healthy
execution budget: moderate
execution budget: low
```

未来可以由多维指标构成：

- token budget remaining
- wall-clock budget remaining
- cost budget remaining
- step hard ceiling remaining（仅作为最终保险）

其中：

- `max_steps`：Runner 层硬熔断；
- resource budget signal：模型侧软提示；
- completion guard：决定是否真正成功。

三者职责不能混淆。

---

## 7. Completion Guard 后续可继续增强

当前 Forge 的 FINISH guard 若失败，会直接返回 `RunStatus.FAILED`。

后续可以考虑更合理的语义：

```text
模型请求 FINISH
    ↓
completion guard 发现缺少必要验证 / 最新测试失败 / 写后未测试
    ↓
如果资源仍足够：
    注入 [COMPLETION REJECTED] + unresolved requirements
    继续 Agent loop

只有：
    guard 通过 → SUCCESS
    或资源耗尽 → INCOMPLETE / MAX_STEPS
```

这比单纯通过 prompt 要求“不要假装完成”更可靠，因为完成条件由框架验证而不是由模型自报。

该增强尚未实现，不应在下一窗口直接修改，先做设计确认。

---

## 8. 当前已完成代码修改

### 8.1 Agent terminal action protocol（前序修复）

- `agent/prompt.py`
- `llm/openai_compat.py`
- `tests/test_openai_terminal_actions.py`

目的：

- `finish` / `give_up` 是内部 terminal action，不是 ToolRegistry 工具；
- OpenAI-compatible function-call / pseudo tool-call / stop-text 路径均正确映射为 `ActionType.FINISH/GIVE_UP`。

### 8.2 Completion guard commit-aware

- `agent/core.py`
- `context/repository_state.py`（复用，未新增第二套实现）
- `tests/test_agent_completion_guards.py`

### 8.3 Step budget warning（当前临时版本）

- `agent/prompt.py`
- `agent/core.py`
- `tests/test_agent_completion_guards.py`

注意：该 warning 已实现，但设计仍处于待重审状态，下一窗口先讨论 wording/触发条件，不直接扩大。

---

## 9. 当前测试状态

截至本日志写入时：

- 代码和回归测试已经 push 到 `dev`；
- 尚未在用户本地 WSL 报告完整 pytest 结果；
- 不得写成“测试已通过”。

建议下一窗口先执行：

```bash
git pull origin dev

python -m pytest -q \
  tests/test_agent_completion_guards.py \
  tests/test_day2.py \
  tests/test_chat.py \
  tests/test_evals.py \
  tests/test_context_policy_agent_ablation.py
```

如果测试失败，先修回归，不跑真实 API。

---

## 10. 下一步真实 API 验证原则

不要立即重跑完整 9-run B2。

在 production fixes + tests 通过后，仅做定向回归：

1. `long-hard-constraint / hybrid_compaction`
   - 验证 commit-aware completion guard；
2. `huge-tool-history / baseline`
   - 验证 Agent 自己 commit 后仍可正常 SUCCESS；
3. `superseded-state / hybrid_compaction`
   - 验证新的低资源提示是否改善及时收尾。

如果第 3 条仍然 `max_steps`：

- 不继续简单增加 `max_steps`；
- 不继续强化“赶紧结束”类 prompt；
- 转而设计 completion-rejection continuation / resource-aware finalization。

---

## 11. 下一窗口必须先读的文件

按优先级：

1. `B2评测与Agent终止策略收口.md`
2. `AGENTS.md`
3. `agent/core.py`
4. `agent/prompt.py`
5. `context/repository_state.py`
6. `tests/test_agent_completion_guards.py`
7. `evals/context_policy_agent_ablation.py`
8. `evals/fixtures/context_policy_agent_cases.json`
9. `evals/results/context_policy_agent_ablation_v2/report.json`
10. `evals/results/context_policy_agent_ablation_v2/raw.jsonl`

如需进一步分析 Context 本身，再读：

- `context/compaction.py`
- `context/tool_pruning.py`
- `context/token_budget.py`

---

## 12. 下一窗口的核心决策问题

下一窗口不要一上来写代码，先回答：

1. `max_steps` 是否只作为 Runner hard ceiling，而不直接暴露精确 Step X/Y？
2. 当前最后三步 warning 是否改成“resource budget low，但未完成不要 FINISH”的中性提示？
3. resource signal 应由 step ratio 触发，还是应该优先基于 token/time/cost？
4. completion guard 失败后，是否应在资源允许时继续 Agent loop，而不是立即 `FAILED`？
5. 在上述语义确定后，再决定是否做第三轮 B2。

核心原则保持：

> 先真正解决任务；资源限制用于控制最坏情况，不得重新定义“任务完成”。
