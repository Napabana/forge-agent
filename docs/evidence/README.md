# Forge Agent Evidence Pack

本目录是 P1-6 / P2 的统一证据入口。目标是让简历和面试中的技术主张能够回链到当前 `dev` 的实现、确定性回归、冻结 benchmark、真实模型小样本或真实端到端案例，同时明确每类证据不能证明什么。

默认离线校验：

```bash
python -m evals.verify_evidence_pack
```

该命令只读取仓库中的证据文件和冻结 JSON；不调用 Provider，不要求 API Key，不访问真实 GitHub，不重跑付费实验，不覆盖 `evals/results`，不修改 fixture，也不修改仓库状态。

## Evidence 分类

- **Implementation Fact**：源码中存在某项实现。只能证明“实现了”，不能自动证明效果、可靠性或成功率。
- **Deterministic Offline Regression**：固定输入下验证确定性 contract 的离线测试。只能证明对应契约被覆盖，不能写成 Agent 成功率。
- **Frozen Offline Benchmark**：固定 fixture、固定协议和冻结结果。数字只适用于该 fixture/protocol。
- **Real-model Small Sample**：真实模型的小样本运行。可报告观察值，但不能当稳定总体性能结论。
- **Real End-to-End Case**：真实外部链路案例。可证明链路实际跑通过，但单案例不能外推成功率。

## Evidence Index

| Claim | Type | Evidence | Reproduce / Check | Result | Limitation |
| --- | --- | --- | --- | --- | --- |
| 同步 ReAct coding Agent 主循环完成 `LLM → Action/ToolCall → Observation → Reflection/termination` | Implementation Fact | `agent/core.py`, `agent/task.py` | `pytest tests/test_agent_completion_guards.py -q` | 主循环、完成性门禁和终止状态已实现 | Agent loop 本身不是 async；实现事实不代表真实任务成功率 |
| CLI / Chat / API / GitHub Issue 通过统一 `ExecutionRunner` 进入 Agent 生命周期 | Implementation Fact + Regression | `agent/runner.py`, `entry/*.py`, `tests/test_runner.py`, `tests/test_failure_harness.py` | `pytest tests/test_runner.py tests/test_failure_harness.py -q` | Runner 统一 direct/isolate、Acceptance 与 Trace 接线 | 不等于四入口真实线上流量验证 |
| Tool lifecycle 为 `validate → pre-hook → permission → tool → post-hook`，并有统一错误分类 | Implementation Fact + Regression | `harness/executor.py`, `harness/hooks.py`, `harness/permission.py`, `tests/test_tool_lifecycle_p0_2.py` | `pytest tests/test_tool_lifecycle_p0_2.py -q` | 生命周期和失败语义有确定性回归 | 不能写成“工具调用永不失败” |
| MCP external capability 接入现有 Tool lifecycle | Implementation Fact / Local Validation Pending | `mcp_integration/manager.py`, `mcp_integration/adapter.py`, `mcp_integration/registry.py`, `tests/test_mcp_integration.py` | `python -m pytest -q tests/test_mcp_integration.py` | 已实现 official SDK v2 client、stdio/Streamable HTTP transport adapter、namespace/schema/effect/permission/Trace/lifecycle 接线；本轮尚未记录用户本地 pytest 通过 | 不能写成 MCP regression 已通过、真实外部 MCP 成功率或对 coding success 的提升 |
| cancellation 为 cooperative cancellation | Implementation Fact + Regression | `agent/core.py`, `harness/executor.py`, `entry/api.py`, `tests/test_tool_lifecycle_p0_2.py`, `tests/test_failure_harness.py` | 同上 | 在 Provider / lifecycle 边界检查 cancel，并返回明确终止语义 | 不会强杀任意正在执行的同步 Provider/Tool 调用 |
| Failure Harness 覆盖 provider/hook/permission/tool/prepare/cancel/completion/acceptance/Trace 故障合同 | Deterministic Offline Regression | `tests/test_failure_harness.py`, `tests/test_failure_harness_isolate.py` | `pytest tests/test_failure_harness.py tests/test_failure_harness_isolate.py -q` | 走生产 `ExecutionRunner → Agent → ToolExecutor` 路径注入故障 | 这是 contract regression，不是 Agent success rate |
| Coding Agent Evaluation Harness 提供 task → isolated trial → production Runner → deterministic graders → artifact/report 的统一 A/B 外壳 | Implementation Fact + Deterministic Offline Regression | `evals/coding_agent/`, `evals/fixtures/coding_agent/suite.json`, `tests/test_coding_agent_eval.py` | `pytest tests/test_coding_agent_eval.py -q` | 8-case suite、Trial/Grader/Report、no-overwrite、not-executed 与 fake-evidence boundary 已有离线回归；用户本地确认相关回归及全量 pytest 通过 | 真实模型 baseline 尚未执行；不能写 Forge Agent success rate、pass@1、token 或 latency 对比 |
| Trace v2 提供 schema v2、run/step/tool correlation、termination/acceptance/delivery 记录和磁盘边界脱敏 | Implementation Fact + Regression | `agent/event_log.py`, `agent/trace_v2.py`, `tests/test_trace_v2.py` | `pytest tests/test_trace_v2.py -q` | append-only audit trace 可 replay 读取 | EventLog 不是确定性执行 replay；本地 token breakdown 是估算 |
| Context Compaction 保留 canonical history，模型视图支持 deterministic pruning + structured semantic compaction + checkpoint lineage | Implementation Fact + Frozen Benchmark + Small Sample | `context/compaction.py`, `context/tool_pruning.py`, `context/structured_compaction.py`, B1/B2 结果 | `python -m evals.verify_evidence_pack` | B1 frozen replay 中 hybrid `7/7`，hard-constraint / recent-raw recall 均 `1.0` | B1 semantic 是 fixture；B2 只有 9 个真实模型 run，不能宣称稳定总体收益 |
| Query-aware Repo Map 改善冻结 commit-history 检索排序 | Frozen Offline Benchmark | `context/repo_map.py`, `evals/repo_map_ablation.py`, `evals/results/repo_map_ablation/report.json` | `python -m evals.verify_evidence_pack` | 12-case：MRR `0.096954 → 0.318750`；budget target recall `0.364914 → 0.635251` | 只代表 12 个冻结 commit-history case，不代表 coding task success rate |
| Repo Map reference counting 热点被优化 | Frozen Offline Performance Experiment | `context/repo_map.py`, `evals/repo_map_ablation.py`, frozen report | `python -m evals.verify_evidence_pack` | 同一冻结协议中 median `35.1176s → 0.4928s`，`71.26×`，semantic hash 等价 | 仅是 reference-count 子步骤和该机器/快照；不是 Agent 端到端 71× |
| Repo Map 已拆成 persistent structural index 与 Query-aware view | Implementation Fact + Regression | `context/repo_index.py`, `context/incremental_repo_map.py`, `context/repository_state.py`, `agent/core.py` | Repo Map P2 tests | SQLite index 可跨进程 warm reuse；Query change 只 rerank；已知 changed path 只更新目标文件 | cold build 当前没有加速；实现事实不代表 Agent success rate |
| Persistent Repo Map 保持冻结 Query-aware ranking / rendering 语义 | Frozen Offline Benchmark + Regression | `evals/repo_map_persistent_benchmark.py`, `evals/results/repo_map_persistent_benchmark/report.json` | `python -m evals.verify_evidence_pack` | strict 12-case：semantic/ranking/visible/rendering 全等价，正式 retrieval 指标 delta=`0` | 只证明冻结协议语义不退化 |
| Persistent Repo Map 的 warm / incremental phase 降低了本次 snapshot 的索引阶段耗时 | Frozen Offline Performance Experiment | `evals/results/repo_map_persistent_benchmark/report.json` | `python -m evals.verify_evidence_pack` | 5-run median：legacy `0.6097s`、cold `0.9605s`、warm `0.2967s`、single `0.1700s`、multi `0.1766s`、full rebuild `0.8045s` | 机器/快照相关；cold build 反而更慢；不是 Agent E2E latency |
| Repo Map A/B/C/D Real Coding Agent harness 已接 production path | Implementation Fact + Deterministic Harness Validation | `evals/repo_map_agent_ablation.py`, `evals/fixtures/repo_map_agent_cases.json`, `tests/test_repo_map_agent_ablation.py` | `python -m evals.repo_map_agent_ablation --validate-only --output /tmp/forge-repo-map-agent-ablation` | No/Static/Query-aware/Incremental 四组固定 fixture/harness 已就绪 | CI 无 provider credential；冻结 report 为 `rows=0`，不能写任何 success/token/latency 对比 |
| Repo Map prompt layout 把 stable rules/tool schema 放在 dynamic repository context 之前 | Implementation Fact + Regression | `agent/prompt.py`, `tests/test_repo_map_prompt_layout.py` | `pytest tests/test_repo_map_prompt_layout.py -q` | 为 prefix reuse 提供结构条件 | 没有真实 provider cached-token 对照，不能宣称 cache hit 提升 |
| Session 持久化支持 resume、版本迁移、冲突检测、secret redaction 和 stale pending recovery | Implementation Fact + Regression | `agent/session.py`, `agent/session_store.py`, `entry/chat.py`, `tests/test_session_store.py` | `pytest tests/test_session_store.py -q` | session state 是恢复真相源；双进程 stale revision 只允许一个 writer 成功 | Trace 不是 Session recovery state；不代表分布式 session service |
| Independent Acceptance 在 Agent history 之外冻结 required/forbidden paths 与 hidden verifier | Implementation Fact + Regression | `agent/runner.py`, `tests/test_runner.py` | `pytest tests/test_runner.py -q` | Agent status 与 acceptance status 分离；Agent 非 success 时 verifier skipped | verifier 失败目前不自动回灌模型继续修复 |
| GitHub Issue → Agent → acceptance → deterministic commit/push/PR 已实现 | Implementation Fact + Regression + Real E2E Case | `entry/github_issue.py`, `tests/test_github_issue_delivery.py`, `docs/changes/2026-09-15/pr-test真实PR改动内容.md`, `Napabana/pr-test#5` | 离线 contract：`pytest tests/test_github_issue_delivery.py -q`；真实案例只检查冻结证据 | acceptance gate、push/pr failure 和 PR retry idempotence 有回归；1 个真实 merged PR | 真实案例只有 1 个，不能写自动 PR 总体成功率；仍由人审合并，无 auto-merge |
| LLMBackend / router 支持 Anthropic、OpenAI 和 OpenAI-compatible provider 接入 | Implementation Fact | `llm/base.py`, `llm/router.py`, backend adapters/tests | 检查源码与 adapter tests | provider/protocol 统一到 `LLMBackend` | 不能据此声称所有 provider 都做过相同规模真实 E2E 验证 |
| Git Worktree 支持隔离工作区的创建、检查、保留/清理 | Implementation Fact + Regression | `runtime/worktree.py`, `agent/orchestrate.py`, `tests/test_worktree_session.py`, `tests/test_orchestrate.py` | 对应 pytest | 能隔离 checkout/workspace 生命周期 | Git Worktree 不是安全沙箱 |
| Docker Runtime 有资源、网络和挂载边界 | Implementation Fact + Regression | `tools/runtime.py`, `tests/test_sandbox.py` | `pytest tests/test_sandbox.py -q` | 默认 1 GiB、2 CPU、`--network none`，并支持只读根/受控挂载 | 不能写“完全安全”；真实 PR #5 案例没有同时启用 Docker isolate |

## 正式数字与适用边界

### Context Policy B1 — Frozen Offline Benchmark

证据：`evals/results/context_policy_benchmark/metadata.json` 与 `report.json`。

- `7 cases × 3 variants = 21 rows`；semantic mode 为 `fixture`。
- hybrid：`7/7` passed；mean hard-constraint recall=`1.0`；mean recent-raw recall=`1.0`。
- `summary_total_tokens=0` 是 fixture summarizer 的结果，不应解读为真实模型总结“零成本”。

机器校验锚点：**B1: 7 cases × 3 variants = 21 rows**

### Repo Map — Frozen Offline Benchmark

证据：`evals/fixtures/repo_map_ablation.json`、`evals/repo_map_ablation.py`、`evals/results/repo_map_ablation/report.json`。

- 12 个 case 来自本仓库真实 commit history；query 使用 commit subject；ground truth 为 parent snapshot 中已存在、由目标 commit 修改的非测试源码文件。
- MRR 从 `0.0969540782` 到 `0.318750`。
- Token budget 内 target recall 从 `0.3649140212` 到 `0.6352513228`。
- reference counting 在同一 snapshot、每实现 5 次计时、semantic hash 等价条件下，median `35.117627s → 0.492843s`，speedup `71.2552×`。

机器校验锚点：**Repo Map MRR: 0.096954 → 0.318750**  
机器校验锚点：**Repo Map budget target recall: 0.364914 → 0.635251**  
机器校验锚点：**Repo Map reference-count median: 35.1176s → 0.4928s (71.26×)**

### Repo Map P2 — Persistent / Incremental Strict Evidence

证据：`context/repo_index.py`、`context/incremental_repo_map.py`、`context/repository_state.py`、`evals/repo_map_persistent_benchmark.py`、`evals/results/repo_map_persistent_benchmark/report.json` 和 `docs/changes/2026-09-16/P2-RepoMap-增量索引与真实任务消融改动内容.md`。

strict 12-case：semantic、full ranking、Token Budget visible-set、rendered Repo Map 均 `12/12` 等价；与冻结 Query-aware 的 MRR / budget recall / recall@1 / recall@3 / recall@5 / mean target rank delta 全为 `0`。

5-run CI phase median：legacy build=`0.6097s`、persistent cold build=`0.9605s`、warm load=`0.2967s`、query rerank=`0.2080s`、single-file update=`0.1700s`、two-file update=`0.1766s`、explicit full rebuild=`0.8045s`。

同时 warm load 重新 parse `0` 文件、single-file update 只 parse `1` 文件、two-file update 只 parse `2` 文件，benchmark 后 working tree clean。

边界：cold build **没有加速**；phase timing 不是 Agent E2E latency；时间值只属于冻结 CI machine/snapshot。

机器校验锚点：**P2 Repo Map equivalence: 12/12 frozen retrieval cases**  
机器校验锚点：**P2 Repo Map phase medians: legacy 0.6097s; cold 0.9605s; warm 0.2967s; single-file 0.1700s; multi-file 0.1766s; full rebuild 0.8045s**

### Repo Map P2 — Real Coding Agent Harness Status

证据：`evals/results/repo_map_agent_ablation/report.json`。

四组已经固定：`no_repo_map`、`static_repo_map`、`query_aware_repo_map`、`incremental_query_aware_repo_map`。

冻结状态：

```text
execution_status = not_executed
real_model_executed = false
rows = 0
reason = provider_credentials_not_available_in_ci
```

这个 artifact 只证明 fixture、四组 variant、production-path harness 和指标采集协议存在；没有提供 Coding Agent solved rate、token、latency 或 cache-hit 结论。

机器校验锚点：**P2 Repo Map Agent ablation: not executed; rows=0**

### Context Policy B2 v3 — Real-model Small Sample

证据：`evals/results/context_policy_agent_ablation_v3/metadata.json` 与 `report.json`。

- model=`deepseek-v4.1-flash`，provider=`openai`，3 cases × 3 variants，每个 cell 仅 1 次运行，共 9 runs。
- baseline / pruning / hybrid solved 分别为 `1/3`、`2/3`、`2/3`。
- 模型非 deterministic，且没有多 seed / repeated trials；因此只报告这 9 次运行的观察值。
- report 字段名虽然包含 `pass_at_1`，这里不能被包装为稳定 benchmark pass@1 或总体成功率。

机器校验锚点：**B2 v3: 3 cases × 3 variants × 1 run = 9 real-model runs**

### Real GitHub Issue → PR Case

冻结案例：`Napabana/pr-test` Issue #4 → PR #5。

- 同一个真实 case 一共记录 4 次尝试，前三次失败、第四次成功；这是同一案例的调试过程，不是 4 个独立 benchmark 样本。
- 成功运行通过目标仓库 15 tests 与仓库外 hidden verifier，随后由 delivery 层 commit、push、create PR；PR #5 后续由人工审阅并 merge。
- 目前正式可引用的真实自动交付案例数为 1。

机器校验锚点：**Real GitHub delivery cases: 1**

### P2-1 Structured Planning — Deterministic Regression

证据：`agent/planning.py`、`agent/core.py`、`tests/test_structured_planning.py`、`tests/test_coding_agent_eval.py`、`docs/changes/2026-09-19/P2-1-Structured-Planning本地回归-DONE.md`。

已实现 typed `ExecutionPlan / PlanStep / PlanRevision`、`planning_mode=off|auto|always`、mutation-before-plan gate、current-plan runtime context、plan lifecycle Trace v2 events，以及 P2-0 `planning` evaluation variant 的真实 architecture mapping。

用户本地最终全量 pytest 已确认通过；最终通过轮次未提供具体 passed 数量或耗时，因此不补造数字。

边界：没有执行 real-model `baseline_react vs planning` A/B，因此不能宣称 Planning 提升 success rate、pass@1、token efficiency 或 latency。

机器校验锚点：**P2-1 Structured Planning: deterministic regression passed; real-model A/B not executed**

### P2-2 Failure-aware Recovery + Replanning — Deterministic Regression

证据：`agent/recovery.py`、`agent/core.py`、`tests/test_structured_recovery.py`、`tests/test_failure_harness.py`、`tests/test_coding_agent_eval.py`、`docs/changes/2026-09-19/P2-2-Failure-aware-Recovery-Replanning本地回归-DONE.md`。

已实现 typed `FailureContext / RecoveryDecision / RecoveryPolicy / RecoveryRuntime`、bounded recovery budget、P2-1 replan runtime gate、compaction-surviving recovery state、Trace v2 recovery events，以及 P2-0 `planning_recovery` architecture mapping。

用户本地专项、关键兼容、全量 pytest 与 Evidence Pack 校验均已明确确认通过；最终通过轮次没有提供具体 passed 数量或耗时，因此不补造数字。

边界：没有执行 real-model `planning vs planning_recovery` A/B，因此不能宣称 Recovery 提升 success rate、pass@1、token efficiency、latency 或故障恢复成功率。

机器校验锚点：**P2-2 Structured Recovery: deterministic regression passed; real-model A/B not executed**
### P2-3 Agent Skills — Deterministic Regression

证据：`skills/catalog.py`、`skills/runtime.py`、`agent/core.py`、`tests/test_agent_skills.py`、`tests/test_coding_agent_eval.py`、`docs/changes/2026-09-19/P2-3-Agent-Skills本地回归-DONE.md`。

已实现 filesystem Skill discovery、project/global override、metadata-only catalog、`skill_load` / `skill_reference_load` progressive disclosure、compaction-surviving SkillRuntime、script non-execution boundary、Trace v2 Skill lifecycle，以及 P2-0 `planning_recovery_skills` architecture/process evaluation 接线。

用户本地专项、关键兼容、package discovery、not-executed Eval 接线、Evidence Pack 与全量 pytest 均已明确确认通过；最终通过轮次没有提供具体 passed 数量或耗时，因此不补造数字。

边界：没有执行 real-model `planning_recovery vs planning_recovery_skills` A/B，因此不能宣称 Skills 提升 coding success rate、pass@1、trigger accuracy、token efficiency、latency 或真实任务效果。

机器校验锚点：**P2-3 Agent Skills: deterministic regression passed; real-model A/B not executed**

### P2-4 MCP Client / Tool Adapter — Deterministic Regression

证据：`mcp_integration/manager.py`、`mcp_integration/adapter.py`、`mcp_integration/registry.py`、`agent/runner.py`、`agent/orchestrate.py`、`tests/test_mcp_integration.py`、`docs/changes/2026-09-19/P2-4-MCP-Client-Tool-Adapter.md`。

已实现 Forge Host → official MCP Python SDK v2 client → remote Tool 的 capability integration；remote Tool 被适配成普通 Forge Tool，调用仍必须经过 ToolRegistry / ToolExecutor / Hook / Permission / cooperative cancel / Trace。stdio 与 Streamable HTTP 复用同一 manager lifecycle；Chat/direct 长会话复用连接，isolate/worktree 每次 run 独立连接，CLI/API/GitHub/Eval 均显式 cleanup。Trace 记录 server capability negotiation 与 tool correlation，但不记录 URL/env/secret；独立 MCP-specific eval case 要求 process trace 真正调用固定 guidance tool。

安全边界：remote ToolAnnotations 默认不可信；只有 server 显式配置 `trust_read_only_annotations=true` 且 tool 声明 read-only 时才映射为 `READ_ONLY`，否则保守映射为 mutation-capable 并进入 CONFIRM。Trace 不记录 MCP env/secret。

用户已在本地完成修复并 push，P2-4 按项目交接约定收口为 DONE。当前可核验修复提交 `f842902e1753bdc69b0217d5aa89033bacd2ae82` 的日志记录定向测试 3 passed、Chat/GitHub Issue 相关测试 25 passed 与 `git diff --check` 通过；用户未提供最终全量 pytest 的 passed 数量、完整 stdout 或耗时，因此不补造数字。

边界：没有执行 real-model `planning_recovery_skills vs planning_recovery_skills_mcp` A/B，因此不能宣称 MCP 提升 coding success/pass@1/token/latency，也不能把 local fixture regression 外推为任意外部 MCP 服务的生产可靠性。

机器校验锚点：**P2-4 MCP: local fix/validation completed and pushed; real-model A/B not executed**


### P2-5 Trajectory-driven Skill Evolution — Implementation Fact / Local Validation Pending

证据：`experience/schema.py`、`experience/trajectory.py`、`experience/candidate.py`、`experience/store.py`、`experience/evaluation.py`、`experience/promotion.py`、`tests/test_skill_evolution.py`、`evals/fixtures/skill_evolution/`、`docs/changes/2026-09-19/P2-5-Trajectory-driven-Skill-Evolution.md`。

已实现 post-run、trajectory-driven、eval-gated Skill improvement pipeline：复用 Trace v2 / TrialResult 作为 canonical artifact，deterministic 提取 verified workflow 与 typed failure→recovery pattern，生成与正式 SkillCatalog 物理隔离的 candidate Skill，再复用 P2-0 Evaluation Harness 做 target / should-trigger / should-not-trigger / non-regression 对照，并由 deterministic PromotionGate 产生 PASS / REJECT / INSUFFICIENT_EVIDENCE / EVALUATION_FAILED。只有持久化 PASS decision 后显式 `promote()` 才能进入现有 project SkillCatalog；用户手工 Skill 不会被静默覆盖。

当前验证边界：ChatGPT 执行环境只完成新模块 `py_compile` 与 standalone core smoke；尚未完成用户本地 P2-5 专项、P2-0/P2-1/P2-2/P2-3/Trace/Failure Harness 回归、Evidence Pack 与全量 pytest，因此本项不是 Deterministic Regression DONE。独立 fixture 证明的是 mechanism design 可测试，不是现实 trajectory 已产生持续智能提升。

边界：没有执行 real-model candidate A/B，没有证明 success rate、pass@1、trigger accuracy、token/step efficiency 或长期 self-improvement 提升；没有在线学习、模型参数训练、当前 run 自改 prompt、自动 promotion、global Skill 自动覆盖。

机器校验锚点：**P2-5 Skill Evolution: implemented; local validation pending; real-model improvement not executed**

## 面试可说 / 不可说

| 能力 | 可以安全说 | 追问证据 | 过度宣称 |
| --- | --- | --- | --- |
| Agent loop | “实现同步 ReAct coding Agent 主循环，并把 finish/completion guard 映射成明确终止状态。” | `agent/core.py`, completion guard tests | “异步高并发 Agent 内核”“真实任务成功率 X%” |
| Tool lifecycle | “统一 validate/pre-hook/permission/tool/post-hook 顺序，并把 policy/tool/infra 失败分层。” | `harness/executor.py`, `tests/test_tool_lifecycle_p0_2.py` | “工具调用无失败”“所有异常都自动恢复” |
| cooperative cancellation | “在 Provider、tool lifecycle 和 step 边界做 cooperative cancel，并保留 Trace 终止语义。” | `agent/core.py`, `harness/executor.py`, failure tests | “可以立即强杀任意同步工具或 Provider 请求” |
| Structured Planning | “在单一 Agent loop 内实现 typed Plan/Step/Revision，支持 off/auto/always、计划生命周期 Trace，并在 context compaction 后持续注入 current plan。” | `agent/planning.py`, `agent/core.py`, `tests/test_structured_planning.py` | “Planning 已证明提升成功率/pass@1/降低 token” |
| Agent Skills | “实现 filesystem Skill catalog 与 progressive disclosure：metadata 常驻、完整 Skill/reference 按需加载，SkillRuntime 跨 compaction 保留；Skill 不绕过 ToolExecutor 执行脚本。” | `skills/catalog.py`, `skills/runtime.py`, `tests/test_agent_skills.py` | “已证明 Skills 提升成功率/trigger accuracy”“Skill script 可直接绕过权限执行” |
| Trajectory-driven Skill Evolution | “基于落盘 Trace/Eval artifact 做 deterministic experience mining，将 candidate Skill 与正式 catalog 隔离，并通过现有 Evaluation Harness + deterministic PromotionGate 后显式 promotion。” | `experience/*`, `tests/test_skill_evolution.py`；当前本地回归待验证 | “Agent 会自主进化”“自动学习越来越聪明”“已证明成功率提升” |
| MCP capability integration | “将 official MCP Python SDK v2 作为 external capability source 接入现有 ToolRegistry/ToolExecutor；remote Tool 继续受 Hook、Permission、Cancel、Planning effect gate 与 Trace 约束，并显式管理 server connection lifecycle。” | `mcp_integration/*`, `agent/runner.py`, `tests/test_mcp_integration.py` | “MCP regression 已通过”“接任意 MCP server 都安全”“MCP 已提升 coding 成功率/pass@1” |
| Failure-aware Recovery | “在单一 Agent loop 内实现 typed FailureContext/RecoveryDecision、bounded recovery 与 plan revision gate；Provider retry、cancel、infra 保持独立语义。” | `agent/recovery.py`, `agent/core.py`, `tests/test_structured_recovery.py` | “已证明提升成功率”“生产级 fault tolerance”“恢复成功率 X%” |
| Error Recovery / Failure Harness | “对 transient provider retry、工具失败 Observation、循环/完成性失败和基础设施异常做了确定性故障回归。” | `tests/test_failure_harness*.py` | “Fault tolerance 达到生产级”“故障恢复成功率 X%” |
| Trace v2 | “用 append-only JSONL 记录 run/step/tool/acceptance/delivery correlation，并在落盘边界递归脱敏。” | `agent/event_log.py`, `agent/trace_v2.py`, trace tests | “Trace 可以确定性重放 Agent 执行” |
| Context Compaction | “canonical history 不被覆盖，模型视图做 deterministic pruning + structured compaction，并有 checkpoint lineage。” | implementation + B1/B2 | “B2 证明稳定提升 2 倍成功率”“总结成本为 0” |
| Repo Map retrieval | “query-aware ranking 在 12-case commit-history 冻结集上把 MRR 0.097 提到 0.319，预算内 target recall 0.365 提到 0.635。” | formal report/script/fixture | “因此 coding task success rate 提升 X%” |
| Repo Map persistent/index | “把 Repo Map 拆成持久化 SQLite 结构索引和 Query-aware 视图；Query 改变只 rerank，changed file 做增量更新，并用 12-case strict equivalence 锁住旧 ranking/rendering 语义。” | `context/repo_index.py`, `context/incremental_repo_map.py`, P2 report | “首次索引更快”“整个 Agent 快 4×/5×/71×” |
| Repo Map phase timing | “当前冻结 CI snapshot 中 warm load / single-file / two-file update 中位约 0.297s / 0.170s / 0.177s，full rebuild 约 0.805s；cold persistent build 约 0.961s，比 legacy 0.610s 更慢。” | P2 frozen report | “所有仓库固定有相同比例加速”“E2E latency 等于这些数” |
| Repo Map Agent ablation | “A/B/C/D production-path harness 已完成，但 CI 无凭据，所以真实模型实验还没执行。” | agent ablation script/report | “四组成功率已经有结论” |
| Prompt Cache layout | “把稳定 tool schema 放到动态 Repo Map 之前，等待真实 provider cached-token 实验。” | prompt layout test | “缓存命中率已提升 X%” |
| Session | “Chat session 独立持久化 history/round/usage/checkpoint，带 revision conflict、migration、redaction 和 stale pending recovery。” | session store tests | “EventLog 就是 session 数据库”“支持分布式强一致 session” |
| Independent Acceptance | “Runner 在模型不可见的独立阶段执行 path contract / hidden verifier，并把 acceptance 与 Agent status 分开。” | `agent/runner.py`, runner tests | “隐藏 verifier 失败后会自动让 Agent 继续修复” |
| GitHub PR delivery | “实现 Issue→Agent→独立验收→确定性 commit/push/PR，并有 1 个真实 merged PR 案例。” | delivery tests + PR #5 case log | “自动 PR 成功率 100%”“已是生产级 bot”“支持 auto-merge” |

## Resume Claim → Evidence Mapping

| 当前候选主张 | 结论 | 建议表述 | Evidence / 边界 |
| --- | --- | --- | --- |
| ReAct coding Agent | KEEP | “实现同步 ReAct coding Agent 主循环，覆盖 ToolCall、Observation、Reflection 与 completion guard。” | `agent/core.py` + completion tests；不要加总体成功率 |
| 多 Provider abstraction | REWORD | “抽象统一 `LLMBackend`，路由 Anthropic、OpenAI 与 OpenAI-compatible provider/protocol。” | `llm/base.py`, `llm/router.py`；不要说所有 provider 都做过同等 E2E |
| Tool Calling | KEEP | “统一 Tool schema/validation、Hook、Permission、execution 与 Observation 生命周期。” | executor/lifecycle tests |
| Structured Planning | KEEP | “实现 typed ExecutionPlan/PlanStep/PlanRevision，并将 current plan 作为 runtime context 注入单一 Agent loop，支持 off/auto/always 与 Trace lifecycle。” | deterministic regression 已通过；没有 real-model A/B，不能写效果提升 |
| Agent Skills | KEEP | “实现 project/global filesystem Skill catalog 与 progressive disclosure，按需加载 Skill/reference，并将当前 Skill state 作为 runtime context 保留。” | deterministic regression 已通过；没有 real-model A/B，不能写 trigger/成功率百分比 |
| Trajectory-driven Skill Evolution | HOLD UNTIL LOCAL VALIDATION | “基于 Trace / Eval artifact 实现 trajectory-driven Skill candidate mining，通过 deterministic promotion gate 检查 target/non-regression/trigger/overhead，显式 promotion 后才进入正式 SkillCatalog。” | Implementation 已落地；本地专项/回归/全量 pytest 尚待用户验证，不能写效果提升或‘自主进化’ |
| MCP Client / Tool Adapter | KEEP | “基于 official MCP Python SDK v2 将外部 MCP Tool 适配进既有 ToolRegistry/ToolExecutor，并统一复用 Permission、Hook、cooperative cancel、Planning effect gate 与 Trace。” | 本地修复/验证已完成并 push；当前无 real-model A/B，不能写效果提升或生产级外部服务可靠性 |
| Failure-aware recovery | KEEP | “实现 typed FailureContext/RecoveryDecision 与 bounded RecoveryPolicy，并把 repeated failure 与 P2-1 plan revision gate 联动。” | deterministic regression 已通过；没有 real-model A/B，不能写效果百分比 |
| Error recovery | REWORD | “实现 transient provider retry、typed failure Observation、loop/completion guard，并用 Failure Harness 做确定性故障回归。” | Failure Harness；不要说生产级容错率 |
| Loop detection | KEEP | “对重复 Action/Observation 指纹与无进展循环做检测和终止/恢复控制。” | `agent/loop_detector.py`, loop tests | 只能写 contract，不写效果百分比 |
| Context compaction | REWORD | “实现 canonical-history-preserving 的 pruning + structured compaction，并用 7-case frozen replay 与 9-run real-model 小样本审计。” | B1/B2；明确小样本边界 |
| Repo Map retrieval | KEEP | “query-aware Repo Map 在 12-case frozen commit-history benchmark 上 MRR 0.097→0.319、budget target recall 0.365→0.635。” | 可写数字，但必须带 12-case/frozen 范围 |
| Repo Map persistent index | KEEP | “将 Repo Map 拆为持久化 SQLite 结构索引与 Query-aware 视图，代码修改按 changed file 增量更新，并以 strict equivalence regression 保证旧排序/渲染语义。” | P2 implementation + 12-case strict report；phase 时间只在追问时使用 |
| Trace | KEEP | “Trace v2 记录 run/step/tool/acceptance/delivery correlation 与脱敏审计事件。” | Trace tests；不要写 deterministic replay |
| Git Worktree | REWORD | “用 Git Worktree 提供独立 checkout 与成果保留/清理生命周期。” | worktree/orchestrate tests；不能称安全沙箱 |
| Docker isolation | REWORD | “提供 Docker Runtime，默认资源限制与断网，并支持只读根和受控挂载。” | runtime/sandbox tests；不要写“完全安全”，真实 PR case 未覆盖 Docker |
| acceptance verifier | KEEP | “在 Agent history 外执行独立 AcceptanceContract / hidden verifier，再决定 delivery gate。” | runner tests + real PR case |
| GitHub Issue → PR | KEEP | “实现确定性交付 contract，并完成 1 个真实 Issue→merged PR 案例。” | delivery tests + PR #5；不能写总体成功率 |
| benchmark 数字 | KEEP WITH SCOPE | 只使用本页冻结 B1 / Repo Map / P2 / B2 数字，并同时写 case/run/protocol 范围 | 不把不同证据层混成一个 Agent 指标 |
| “Agent 总体成功率 X%” | **INSUFFICIENT EVIDENCE** | 不写 | 当前没有足够规模的独立真实任务 benchmark |
| “Repo Map 让 Agent 成功率提升 X%” | **INSUFFICIENT EVIDENCE** | 不写 | P2 A/B/C/D real-model report 当前 `rows=0` |
| “Prompt Cache 命中率提升 X%” | **INSUFFICIENT EVIDENCE** | 不写 | 只有 layout regression，没有真实 provider usage 对照 |
| “production ready / 生产级可靠性” | **INSUFFICIENT EVIDENCE** | 不写 | regression coverage ≠ production reliability |
| “自动 PR 成功率 100%” | **INSUFFICIENT EVIDENCE** | 不写 | 只有 1 个真实案例，且同案例经历 4 次调试尝试 |
| “B2 证明 Context Policy 稳定提升成功率” | **INSUFFICIENT EVIDENCE** | 不写 | n=3 cases / variant、每 cell 单次、模型非 deterministic |

## 证据使用规则

任何简历或面试数字必须同时携带它所属的证据层和样本范围。`Implementation Fact ≠ Regression Coverage ≠ Offline Benchmark ≠ Real-model Experiment ≠ Production / Overall Success Rate`。

Repo Map 额外遵守：

```text
retrieval quality
≠
index phase performance
≠
Agent E2E success / latency
```

如果未来新增证据，应先冻结 fixture/protocol/result，再更新本索引与只读校验器；不得为了简历表述反向制造 benchmark 数字。
