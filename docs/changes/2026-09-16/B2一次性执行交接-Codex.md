# Forge Agent B2 一次性执行交接（给本地 Codex）

## 任务

在本地仓库中一次性完成 Forge Agent 的 B2 termination 收口、回归测试、3 个真实定向
run、B2 v3 完整 9-run、结果报告和交接更新。

这是对下文明确列出的 B2 范围的实施授权。完成基线核对后，不需要再为 B2-T1～T4 的
生产代码、对应测试或 B2 v3 实验逐项停下来请求确认。只有遇到“停止条件”中列出的真实
阻塞，或者必须扩大到本文件未授权的范围时，才停止并向用户报告。

不要在 B2 完成后自动开始 P0/P1 的下一批生产代码。

## 仓库与基线

- 仓库：`https://github.com/Napabana/forge-agent`
- 本地 Windows：`E:\2806\forgeAgent\forge-agent`
- 本地 WSL：`/mnt/e/2806/forgeAgent/forge-agent`
- 分支：`dev`
- 接续基线：`ad5546b619916c3b8c16ca2cd21868a6b56368e3`
- WSL 环境：`source ~/.venvs/forge-agent/bin/activate`
- 私有远程必须保留：`git@Napabana:Napabana/forge-agent.git`

先在仓库根目录执行并记录：

```bash
git status --short --branch
git log -5 --oneline --decorate
git remote -v
git stash list
git rev-parse HEAD
git merge-base --is-ancestor ad5546b619916c3b8c16ca2cd21868a6b56368e3 HEAD
```

规则：

- 保留所有用户已有修改，不得 `reset --hard`、`clean` 或 checkout 覆盖文件。
- 不得 drop/apply 未经指定的 stash。
- 不得修改、提交或还原 `config/default.yaml`。
- 不得读取、打印、复制或写入真实 API Key；只使用已配置的 `${KRILL_API_KEY}`。
- 不得 pull/rebase/push，除非用户另行明确要求。
- 如果当前 HEAD 是接续基线的正常后继，且已有修改属于本任务，继续并记录；如果历史已
  分叉、缺失关键基线或用户修改与目标文件发生无法安全合并的冲突，则停止。

## 必读顺序

完整阅读，不要只依赖文件名或搜索片段：

1. `AGENTS.md`
2. `B2评测与Agent终止策略收口.md`
3. `agent/core.py`
4. `agent/prompt.py`
5. `agent/task.py`
6. `agent/runner.py`
7. `agent/event_log.py`
8. `context/repository_state.py`
9. `context/token_budget.py`
10. `tests/test_agent_completion_guards.py`
11. `tests/test_runner.py`
12. `tests/test_evals.py`
13. `tests/test_context_policy_agent_ablation.py`（若实际文件名不同，以 `rg --files` 为准）
14. `evals/context_policy_agent_ablation.py`
15. `evals/fixtures/context_policy_agent_cases.json`
16. `evals/results/context_policy_agent_ablation_v2/report.json`
17. `evals/results/context_policy_agent_ablation_v2/raw.jsonl`
18. `TODO-P0-P1.md` 和 `Forge-Agent-P0-P1-实施计划.md`

如果 completion/compaction 控制流需要，再读：

- `context/compaction.py`
- `context/tool_pruning.py`
- `context/history.py`

先用源码确认下面每个事实；若日志与源码冲突，以当前 `dev` 源码为准，并在最终报告中指出。

## 已冻结的设计，不再重新讨论

### 1. Completion 与 termination

- `FINISH` 只是完成检查请求。
- Completion Guard 通过才返回 `SUCCESS`。
- 可恢复的 guard rejection 且仍有下一次 model step：把 unresolved requirements 写入
  canonical History 和 Trace，然后继续工作。
- guard rejection 发生在最后可用 step：返回 `INCOMPLETE`，reason 为
  `resource_exhausted/max_steps`。
- 真正不可恢复的 Provider、基础设施或内部错误仍为 `FAILED`。
- Hidden verifier 保持在 Agent 外部，不反馈给 Agent。

### 2. 结果状态

新的结果必须能区分：

```text
SUCCESS
INCOMPLETE
FAILED
GAVE_UP
CANCELED
```

同时结构化记录 `termination_reason`，资源耗尽时记录 `resource_reason=max_steps`。不要删除
旧枚举或字段而不审计序列化、Runner、报告和旧结果读取；必要时增加兼容映射，但 B2 v3
不得继续把 max-steps exhaustion 当成普通 `FAILED`。

Loop Detector 由框架主动停止时，不得继续伪装成模型显式 `GAVE_UP`；用
`INCOMPLETE + loop_detected` 或当前结构下等价且清晰的表达。

### 3. Resource warning

保留最后三轮触发，但所有轮次使用同一份 `[RESOURCE BUDGET LOW]`，不得暴露精确
`3/2/1` 或 `Step X/Y`，不得要求模型为了 deadline 立即 FINISH。

语义固定为：未满足真实完成条件不得声称完成；减少可选探索、cleanup、重复检查和无必要
commit；优先最高价值 unresolved requirement；已经完成并充分验证时直接返回最终结果。

该 warning 只加入当前 request，不进入 canonical History。

### 4. 本轮资源范围

`max_steps` 保持 hard ceiling。不要在本轮加入：

- max total tokens；
- wall-clock deadline；
- cost budget；
- 多维 resource scoring；
- `config/default.yaml` 新配置。

现有 `context/token_budget.py::TokenBudget` 是单次请求上下文预算，不是整次 run 的 token
消费上限，不能混用。

## 允许修改的最小文件面

预期可以修改：

| 文件 | 目的 |
| --- | --- |
| `agent/core.py` | guard rejection 继续执行；max steps/loop 的新终止语义；ephemeral warning 注入 |
| `agent/prompt.py` | 中性资源提示；completion rejection 渲染 |
| `agent/task.py` | `INCOMPLETE` 与结构化 termination/resource reason；必要兼容 |
| `agent/event_log.py` | 仅在需要时增加 `completion_rejected` / `task_incomplete` 事件 |
| `agent/runner.py` | 仅在需要时透传新状态并确保 acceptance/delivery 不放行 |
| `evals/context_policy_agent_ablation.py` | 记录新终止字段和 rejection 统计，输出 v3 报告 |
| `evals/report.py` | 仅当通用报告必须兼容新字段时修改 |
| 相关 tests | 固定上述行为和旧结果兼容 |
| `AGENTS.md`、TODO/实施计划、本轮日志 | 按真实结果更新交接 |

如果实现必须修改表外生产文件，先判断是不是当前设计不可避免的直接依赖。只有很小且直接的
兼容修改可继续，并在日志解释；涉及新能力或架构扩张时停止。

新增或修改代码必须遵循 `AGENTS.md`：补中文注释，能清晰单行表达的代码不要无故拆行。

## 实施步骤

### A. 先做源码审计

在动代码前建立简短检查表，确认：

- `RunStatus`、`RunResult`、Event 类型和 JSON 序列化的所有引用位置；
- `Agent.run()` 中 FINISH、fatal error、guard、loop detector、cancel 和 for-loop exhaustion 的
  当前返回路径；
- `ExecutionRunner` 何时执行 hidden acceptance 和 delivery；
- warning 如何追加到 request，以及 canonical History 的写入位置；
- ablation 脚本的真实 CLI、v2 参数、输出字段和目录覆盖行为。

可用 `rg`，不要凭记忆修改。

### B. 实现 B2-T1：可恢复 completion rejection

把现有 completion guard 的纯字符串错误整理成稳定 reason code 与人类可读 detail。至少
覆盖当前已有条件：

```text
REQUIRED_CHANGE_MISSING
REPOSITORY_UNCHANGED
REQUIRED_TEST_MISSING
LATEST_TEST_FAILED
FINAL_STATE_UNVERIFIED
```

如果当前 fatal infrastructure error 共用 `verification_error`，先拆开：不可恢复故障不能被
当成普通 rejection 后反复继续。

当 Agent 在仍有下一步时返回 `FINISH` 且 guard 拒绝：

1. 记录 `completion_rejected` Trace，包含 code/detail/step；
2. 使用框架内部控制消息把 unresolved requirements 加入 canonical History，不冒充原始用户
   文本；若消息模型没有独立 internal role，沿用项目已有控制消息惯例并用测试固定选择；
3. `continue` 下一轮，不返回 `FAILED`；
4. 不改变 `successful_write`、最新测试状态、repo fingerprint 等真实执行状态。

最后一步发生 rejection 时不再额外调用模型，循环耗尽后统一形成 `INCOMPLETE`。

### C. 实现 B2-T2：终止状态

- 增加 `INCOMPLETE`。
- 给结果增加最小结构化 termination/resource reason，默认值保证旧调用方可构造。
- 将正常 for-loop exhaustion 映射到 `INCOMPLETE + resource_exhausted/max_steps`。
- 将框架 loop detector 终止映射到 `INCOMPLETE + loop_detected`；只有 Agent 显式动作才是
  `GAVE_UP`。
- `SUCCESS` 使用 `completion_satisfied`；cancel 和不可恢复失败保留各自清晰原因。
- 审计报告、JSONL、CLI/API/Chat/GitHub 入口是否依赖 `RunStatus.MAX_STEPS`。保持必要读取
  兼容，不扩大为 schema 迁移工程。
- Runner 只有 `SUCCESS` 才执行/通过 acceptance 与 delivery；`INCOMPLETE` 必须 skipped 或
  not-delivered。

### D. 实现 B2-T3：中性 warning

将当前倒计时文本替换为无数字的 `[RESOURCE BUDGET LOW]`。可以保留内部
`remaining <= 3` 判断，但模板不得接收或渲染剩余步数。确认它仍只存在于本轮构建后的
request messages 中，不写入 History。

### E. 补 B2-T4 测试

至少增加或更新以下测试：

1. `write → FINISH rejected(required test) → test PASS → FINISH → SUCCESS`；
2. `FINISH rejected at hard ceiling → INCOMPLETE/resource_exhausted/max_steps`；
3. `latest test failed → rejection → 修复或重新测试 → SUCCESS`；
4. `write after successful test → rejection(FINAL_STATE_UNVERIFIED) → retest → SUCCESS`；
5. Agent 自行 commit 后 FINISH 的 commit-aware 回归继续通过；
6. fatal infrastructure error 不被误判成可恢复 rejection；
7. loop detector 与显式 `GIVE_UP` 状态不同；
8. warning 不包含剩余步数、不进入 canonical History；
9. `INCOMPLETE` 不触发 acceptance/delivery；
10. 评测 JSONL/report 能读取旧 v2 结果并输出新字段默认值。

优先扩展已有测试文件，避免为每个断言创建碎片化文件。

## 测试执行策略

先激活 WSL venv：

```bash
cd /mnt/e/2806/forgeAgent/forge-agent
source ~/.venvs/forge-agent/bin/activate
```

第一批：

```bash
python -m pytest -q tests/test_agent_completion_guards.py tests/test_runner.py
```

然后用 `rg --files tests` 与必要的 `pytest --collect-only` 找到真实的 eval/context-policy
测试文件，运行：

- eval serialization/report；
- context policy ablation；
- 受 `RunStatus`、Runner acceptance、CLI/API/Chat/GitHub 状态展示直接影响的最小回归。

不要机械运行全仓库测试来代替风险判断；但如果 `RunStatus` 搜索表明影响面广，完成所有定向
批次后可以运行一次全量 `python -m pytest -q`。只运行一次，并如实记录首次结果；修复后只
重跑失败节点和直接受影响批次，不为了整齐数字反复全量运行。

先做 `git diff --check`，但如果它只命中受保护的 `config/default.yaml` 既有问题，记录并
跳过该文件，不得修改它。

## 真实 API 实验

### 实验前检查

1. 用脚本 `--help`、v2 raw/report 和旧日志恢复 B2 v2 的真实参数。
2. 固定与 v2 相同：模型、temperature/seed（如果支持）、`budget_tokens=8000`、
   `max_steps=12`、fixture、timeout、Context Policy 实现。
3. 确认输出目录不会覆盖 v2。
4. 只确认 `${KRILL_API_KEY}` 是否存在，不打印值。
5. 先执行脚本现有的无付费 dry-run/列举模式（若有）；没有就不要自行增加无关功能。

### 三个定向 run

按顺序执行并保存到独立的 v3 staging/targeted 位置：

1. `long-hard-constraint / hybrid`
2. `huge-tool-history / baseline`
3. `superseded-state / hybrid`

每个 run 后立即检查：原始 JSONL 是否落盘、Trace 是否存在、fixture diff、Agent status、
termination reason、verifier status、token/latency 字段是否齐全。不要因任务结果不好而调
prompt、预算或 fixture。

如果定向 run 暴露 termination correctness 缺陷，暂停后续付费 run，修复并只重跑受影响
测试；是否重跑该真实样本必须在日志中明确标为“修复验证”，不能混入原始 pass@1 样本。

### 完整 B2 v3

三条定向验证没有发现 correctness 阻塞后，用脚本真实 CLI 一次执行完整矩阵：

```text
cases: long-hard-constraint, huge-tool-history, superseded-state
variants: baseline, pruning, hybrid
runs per cell: 1
output: evals/results/context_policy_agent_ablation_v3/
```

不要覆盖 `context_policy_agent_ablation_v2/`，不要把 targeted 修复验证样本偷偷当作完整矩阵
中的样本，除非脚本本来就明确以一次全新完整矩阵重新采样。

Provider 空响应、超时或基础设施错误要保留原始证据并计入/单列，不得静默重试以改变
pass@1。若脚本已有明确且固定的请求级重试策略，保持不变并记录。

## 报告与验收

在 `evals/results/context_policy_agent_ablation_v3/` 中生成或更新：

- `raw.jsonl`
- `report.json`
- `report.md`（如果脚本现有约定支持）

报告至少给出：

| 指标 | 要求 |
| --- | --- |
| strict pass@1 | Agent `SUCCESS` 且 hidden verifier passed |
| verifier pass | 独立列出，不把 incomplete 当 success |
| tokens | input/output/total，保持 v2 口径 |
| latency | 每格原始值和 variant 汇总 |
| termination | status、termination reason、resource reason |
| rejection | 次数与 reason code 分布 |
| evidence | Trace、diff、verifier 状态的可定位路径 |

生成 v2→v3 对照，至少回答：

1. commit-aware bug 污染的两个样本是否恢复正常；
2. `superseded-state` 是否仍出现 verifier passed 但 max-steps incomplete；
3. strict pass@1 与 verifier pass 的差距还有哪些原因；
4. baseline/pruning/hybrid 的 token、latency、成功率信号是否仍存在；
5. 哪些结论因 `n=3` 和模型非确定性不能外推。

不要求 Hybrid 获胜，不为了结果调整 benchmark。

## 文档收尾

完成后：

1. 新建一份符合 `AGENTS.md` 的本轮改动日志，例如：
   `B2终止语义与v3评测改动内容.md`。
2. 日志记录：目标、真实行为变化、全部修改文件、每条测试命令与首次/重跑结果、真实 API
   命令（隐去凭据）、9-run 结果、已知边界、Git 状态。
3. 更新 `AGENTS.md` 的“当前状态/最后交接”，但不要强制提交被忽略的本地 Markdown。
4. 对照 `TODO-P0-P1.md` 和实施计划，只勾选有代码与测试/实验证据的条目；不要顺手实现
   P0/P1 下一批。
5. 最后再次记录：

```bash
git status --short --branch
git diff --stat
git diff --check
git log -3 --oneline --decorate
git remote -v
git stash list
```

6. 不自动 commit 或 push。给用户一份简短最终摘要，并指向日志和 v3 report。

## 停止条件

以下情况停止，不要擅自绕过：

- 当前分支/历史与基线分叉，无法确认应在哪个版本修改；
- 用户已有改动与目标文件发生无法安全合并的冲突；
- `${KRILL_API_KEY}` 缺失或 Provider 持续不可用；
- 实验将产生无法判断或明显超出既定矩阵的额外费用；
- 必须修改 `config/default.yaml`、远程、stash 或执行 push/rebase 才能继续；
- correctness 修复要求扩展到完整 Resource Manager、hidden-verifier feedback 或本文件明确
  排除的新能力。

停止时也必须保留已完成代码、测试和实验原始输出，并写清楚准确恢复命令与下一步。

## B2 后续但不在本次自动执行范围

B2 完成后建议顺序：

```text
TODO reconciliation
→ P0-2 / P0-3 可靠性与可观测性尾项
→ 少量 P0-1 尾项
→ P1-2 Compaction 正确性闭环
→ P1-4 Harness 扩充
→ P1-5 Repo Map 收口
→ P1-6 隔离图、可重算报告与 Claim 账本
```

先向用户报告 B2 v3 的真实结果和下一批拟修改文件，再开始后续工作。
