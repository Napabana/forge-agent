# P2 Stage 1：Planning v2 / Recovery v2 基础设施收口

日期：2026-09-20

状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

实现起点：`dev@509ad31099639e84c9e8a06114b8df58100840a6`

实现链：

- `d6da718ea9957c8153cd33a0f541adbb137d21b3` — Planning/Recovery control-plane 主实现
- `49d01e502d2673e59e07dd13b260996a53ddaf8e` — strict schema capability / product entry wiring 修正
- `f850097f625817fe9868c97527e1f0b2444ab5ca` — deterministic regression + default config

最终交接以当前 `dev` 最新 HEAD 为准。

## 1. 本轮目标

Stage 1 已经通过真实模型 E2E，当前不继续扩新功能。本轮只收口 Stage 1 暴露出的基础设施债务：

1. Planning Runtime-owned step identity
2. idempotent `plan_step_update`
3. dynamic control surface / dynamic step-id schema
4. Provider-aware strict structured calling
5. 减少 plan bookkeeping 由模型维护
6. Recovery per-category budget + global hard ceiling
7. Semantic progress signal

不实现新的 Planner、第二套 Recovery loop、Trace v3、Memory、Multi-Agent 或新的 Skill 系统。

## 2. 实现前真实链路

### Planning

`agent/planning.py` 原实现同时让模型维护：

- step id
- step status
- revision 后完整 step state

`PlanningRuntime.schemas()` 又静态暴露：

- `plan_create`
- `plan_step_update`
- `plan_revise`

因此模型在 runtime 已经知道 plan state 的情况下仍需要重复提供 bookkeeping 字段，真实 OpenAI-compatible E2E 中出现过缺少 `step_id/reason/goal/steps` 等 malformed control call。

### Recovery

`RecoveryPolicy` 虽然已有 category counter，但所有 failure 先消耗同一个 `_attempts / recovery_max_attempts`。一次 Tool failure、一次 NO_PROGRESS、一次 Test failure 会互相挤占同一个小预算。

### Progress

旧 no-progress 主体围绕 repository edit / test state。Skill load、Plan create/revise、新 test evidence 并不能稳定表达“任务状态已经前进”，而普通重复读取又不应无限刷新 progress。

### Provider schema

OpenAI-compatible Chat adapter 只发送 `parameters`；Responses adapter 固定 `strict=False`。Provider protocol capability 与 model token capability 也没有独立抽象。

另外，本轮复查发现：Core 的 system prompt 会包含 Planning/Skill control schema，但真正传给 backend 的 `tools` 原先只来自 `ToolRegistry`。因此 control schema 的 prompt 表达与 provider function-calling surface 并不完全一致。

## 3. Planning v2

主要文件：

- `agent/planning.py`
- `agent/core.py`
- `evals/coding_agent/graders.py`

### 3.1 Runtime-owned step identity

模型的新 `plan_create` schema 不再要求 `id/status`。模型只提供语义字段：

- `goal`
- `description`
- `targets`
- `verification`

Runtime 根据 description 确定性生成可读 identity：

- ASCII description → lowercase slug
- duplicate slug → `-2/-3/...`
- 无可用 ASCII → `step-N`
- 最大 64 字符

旧 deterministic script 如果仍提供 `id`，Runtime 保留 alias compatibility，但新的 provider schema 不再要求模型生成 id。

### 3.2 Dynamic control surface

无 plan：

```text
plan_create
```

已有 plan：

```text
plan_step_update
plan_revise
```

`plan_step_update.step_id` 的 JSON Schema enum 每轮从当前 Runtime plan 生成，因此 Provider 看到的合法 identity 与当前 plan state 同步。

### 3.3 Runtime-owned bookkeeping

`plan_revise` 的新 schema 不要求：

- step id
- step status
- plan version

Runtime 负责：

- version increment
- previous_version
- revision_reason
- compatible step identity carry-forward
- compatible terminal state carry-forward

语义相同 step 通过 normalized description 保留 identity；旧 payload 若显式带旧 runtime id，也继续兼容。

### 3.4 Idempotent update

相同 terminal state 的重复确认：

```text
completed -> completed
skipped   -> skipped
```

返回 accepted no-op：

```json
{
  "idempotent": true,
  "state_changed": false
}
```

真正回退：

```text
completed -> in_progress
skipped   -> in_progress
```

仍拒绝。

Eval 的 `plan_step_completed_count` 只统计真实 `state_changed=true` 的 transition，重复确认不会膨胀 metric。

## 4. Recovery v2

主要文件：

- `agent/recovery.py`
- `agent/core.py`

`recovery_max_attempts` 现在表示 **per-category recovery budget**。

例如：

```text
tool_failure       1/4
no_progress        1/4
test_failure       1/4
```

互不占用彼此的小预算。

同时保留 global hard ceiling。默认 derived ceiling 为：

```text
global_max_attempts = recovery_max_attempts * 3
```

这不是新的无限 retry 机制；它只把“同类 bounded budget”和“整次 run 的 hard ceiling”拆开。

Trace payload 现在同时提供：

- `category_occurrence`
- `category_max_attempts`
- `global_attempt`
- `global_max_attempts`
- 旧 `attempt/max_attempts` 继续兼容

Infrastructure failure 仍服从既有 fatal contract，不被普通 Recovery budget 改写。

## 5. Semantic Progress

新增 bounded deterministic evidence tracker。

以下事件可以形成确定性的 semantic progress：

- repository content fingerprint 真变化
- 首次 `skill_loaded`
- 真正 `plan_created`
- 真正 `plan_revised`
- 新的 test evidence

以下行为不能无限刷新：

- 重复 `skill_load`
- Planning idempotent no-op
- 重复完全相同的 test evidence
- 普通 file_read / read-only browsing

测试 evidence 使用 bounded fingerprint 去重，不把任意自然语言 reasoning 当 progress。

Completion Guard 原有 repository/test semantics 不变；pending replan 仍不覆盖 FINISH 的 Completion Guard authority。

## 6. Provider-aware strict structured calling

新增：

- `llm/tool_schema.py`

修改：

- `llm/openai_compat.py`
- `llm/openai_responses.py`
- `llm/router.py`
- `config/schema.py`
- `entry/cli.py`
- `entry/api.py`
- `entry/github_issue.py`

### 6.1 capability 边界

strict structured calling 属于 **backend/protocol capability**，不放入 `ModelCapabilities`。后者继续只描述：

- context window
- model max output

新配置：

```yaml
llm:
  strict_tool_schema: auto
```

支持：

- `auto`
- `on`
- `off`

### 6.2 兼容代理默认安全

`auto` 不猜测兼容 gateway 是否完整支持 strict JSON Schema。

因此当前默认 OpenAI-compatible proxy 继续：

```text
strict = off
```

只有明确验证后配置：

```yaml
strict_tool_schema: on
```

才发送 strict contract。

### 6.3 Strict schema conversion

当明确启用时，OpenAI Chat / Responses schema 会：

- 对 object 设置 `additionalProperties: false`
- 递归使所有 properties 出现在 `required`
- Forge 原本 optional 字段转换为 nullable

例如原 schema：

```json
{
  "properties": {
    "cmd": {"type": "string"},
    "cwd": {"type": "string"}
  },
  "required": ["cmd"]
}
```

strict provider schema 会允许：

```json
"cwd": {"type": ["string", "null"]}
```

Provider 返回 `cwd: null` 后，Core 在 Runtime validation 前根据 **原始 Forge schema** 去掉 optional-null placeholder，因此旧工具仍能使用自己的默认值。

Runtime validation 仍保留；strict provider output 不被当作可信边界。

### 6.4 Anthropic

Anthropic 继续使用原生：

```text
input_schema
```

不把 OpenAI strict 字段混入 Anthropic payload。

## 7. Provider-visible schema 与 Runtime schema 统一

新增 `Agent._active_tool_schemas()` 作为单一来源。

现在同一轮：

```text
ToolRegistry schemas
+ PlanningRuntime.schemas()
+ SkillRuntime.schemas()
        ↓
system prompt
        +
provider tools
```

两边一致。

这使 dynamic step-id enum / strict schema 真正进入 provider function-calling surface，而不是只存在于 prompt 文本。

## 8. Regression

新增/扩展：

- `tests/test_structured_planning.py`
- `tests/test_structured_recovery.py`
- `tests/test_agent_skills.py`
- `tests/test_tool_schema_strictness.py`

覆盖：

- runtime-generated step id
- duplicate id suffix
- non-ASCII fallback
- max length
- deterministic replay
- no-plan / has-plan control surface
- dynamic step-id enum
- terminal idempotency
- illegal rollback
- revision identity / terminal progress carry-forward
- Eval no double-count
- unrelated recovery categories independent budget
- same-category exhaustion
- global hard ceiling
- plan create semantic progress
- plain read stagnation
- new test evidence / duplicate test evidence
- first Skill load vs duplicate Skill load
- strict schema recursive normalization
- optional-null Runtime normalization
- proxy auto safe default
- explicit strict on/off
- OpenAI Chat / Responses protocol shape
- Anthropic protocol shape
- config parsing
- malformed payload Runtime rejection

## 9. 当前验证状态

当前 ChatGPT 容器无法解析 `github.com`，因此无法 clone 当前候选 commit；仓库当前也没有可由现有 connector 直接启动的新 workflow。

所以本轮 **没有声明 pytest 已通过**。

本地验证顺序：

```bash
python -m pytest -q \
  tests/test_structured_planning.py \
  tests/test_structured_recovery.py \
  tests/test_agent_skills.py \
  tests/test_agent_completion_guards.py \
  tests/test_cli_isolate.py \
  tests/test_tool_schema_strictness.py \
  tests/test_openai_responses.py \
  tests/test_openai_terminal_actions.py \
  tests/test_model_aware_token_budget.py \
  tests/test_api.py \
  tests/test_chat.py \
  tests/test_github_issue_delivery.py
```

通过后：

```bash
python -m pytest -q
```

在本地专项 + 全量 regression 都通过前，本轮状态保持：

```text
IMPLEMENTED / LOCAL VALIDATION PENDING
```

不得补造 passed 数量或把 implementation 写成 DONE。

## 10. 真实模型 E2E 计划

继续复用：

```text
Napabana/pr-test
branch: forge-p2-skill-demo
```

不要修改 Stage 1 已用 fixture。

建议新 clone：

```bash
git clone --branch forge-p2-skill-demo --single-branch \
  https://github.com/Napabana/pr-test.git pr-test-planning-recovery-v2
```

然后：

```bash
python -m entry.cli run \
  --repo ../pr-test-planning-recovery-v2 \
  --task "Fix the normalize_label regression with the smallest correct change. Follow the repository-specific release verification contract. Do not create a Git commit." \
  --no-stream
```

重点观察 Trace：

1. `plan_create` 不再要求模型生成 id/status；
2. step identity 由 Runtime 产生且后续 update 使用同一 identity；
3. `plan_step_update` before create 不再出现在正常 provider control surface；
4. terminal duplicate 若出现应成为 no-op，而不是 `PLAN_REJECTED`；
5. `skill_loaded / plan_created / plan_revised / new test evidence` 不应被立即误判为 NO_PROGRESS；
6. Tool / NO_PROGRESS / Test failure 的 category occurrence 独立计数；
7. Completion Guard 仍是最终 completion authority；
8. 当前 compatible gateway 在 `strict_tool_schema:auto` 下不应被发送未验证的 strict contract。

## 11. Before / After 比较口径

Stage 1 原始真实 E2E 已知事实：

- 18 steps
- 122,597 tokens
- 98.6s
- SUCCESS
- completion_satisfied

本轮真实 E2E 尚未执行，因此 **暂不填写新的 token / step / latency 数字，也不宣称性能提升**。

本轮验证后应比较：

| 维度 | Stage 1 原始 E2E | Planning/Recovery v2 |
|---|---:|---:|
| status | SUCCESS | 待测 |
| steps | 18 | 待测 |
| tokens | 122,597 | 待测 |
| wall time | 98.6s | 待测 |
| malformed planning controls | 真实出现过 | 待测 |
| duplicate terminal rejection | 可能出现 | 应为 idempotent no-op |
| unrelated recovery budget coupling | 全局共享 | per-category + global ceiling |
| Skill/Plan semantic progress | 不完整 | 已实现确定性 signal |

不能从单次 E2E 推导 success-rate、pass@1 或总体性能结论。

## 12. 未触碰边界

本轮没有：

- 新增第二套 Planner
- 新增第二套 Recovery loop
- 修改 Completion Guard authority
- 修改 ToolExecutor / Permission / Hook / Cancel lifecycle
- 修改 Skill progressive disclosure 模型
- 修改 Trace v2 canonical source
- 修改 B1/B2 fixture
- 覆盖历史 `evals/results`
- 实现 Trace v3 / Memory / Multi-Agent

当前下一步只剩：**本地 deterministic regression → 全量 pytest → 真实模型 E2E → 补验证 DONE 日志**。
