# Forge Agent Evidence Pack

本目录是 P1-6 的统一证据入口。目标是让简历和面试中的技术主张能够回链到当前 `dev` 的实现、确定性回归、冻结 benchmark、真实模型小样本或真实端到端案例，同时明确每类证据不能证明什么。

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
| cancellation 为 cooperative cancellation | Implementation Fact + Regression | `agent/core.py`, `harness/executor.py`, `entry/api.py`, `tests/test_tool_lifecycle_p0_2.py`, `tests/test_failure_harness.py` | 同上 | 在 Provider / lifecycle 边界检查 cancel，并返回明确终止语义 | 不会强杀任意正在执行的同步 Provider/Tool 调用 |
| Failure Harness 覆盖 provider/hook/permission/tool/prepare/cancel/completion/acceptance/Trace 故障合同 | Deterministic Offline Regression | `tests/test_failure_harness.py`, `tests/test_failure_harness_isolate.py` | `pytest tests/test_failure_harness.py tests/test_failure_harness_isolate.py -q` | 走生产 `ExecutionRunner → Agent → ToolExecutor` 路径注入故障 | 这是 contract regression，不是 Agent success rate |
| Trace v2 提供 schema v2、run/step/tool correlation、termination/acceptance/delivery 记录和磁盘边界脱敏 | Implementation Fact + Regression | `agent/event_log.py`, `agent/trace_v2.py`, `tests/test_trace_v2.py` | `pytest tests/test_trace_v2.py -q` | append-only audit trace 可 replay 读取 | EventLog 不是确定性执行 replay；本地 token breakdown 是估算 |
| Context Compaction 保留 canonical history，模型视图支持 deterministic pruning + structured semantic compaction + checkpoint lineage | Implementation Fact + Frozen Benchmark + Small Sample | `context/compaction.py`, `context/tool_pruning.py`, `context/structured_compaction.py`, B1/B2 结果 | `python -m evals.verify_evidence_pack` | B1 frozen replay 中 hybrid `7/7`，hard-constraint / recent-raw recall 均 `1.0` | B1 semantic 是 fixture；B2 只有 9 个真实模型 run，不能宣称稳定总体收益 |
| Query-aware Repo Map 改善冻结 commit-history 检索排序 | Frozen Offline Benchmark | `context/repo_map.py`, `evals/repo_map_ablation.py`, `evals/results/repo_map_ablation/report.json` | `python -m evals.verify_evidence_pack` | 12-case：MRR `0.096954 → 0.318750`；budget target recall `0.364914 → 0.635251` | 只代表 12 个冻结 commit-history case，不代表 coding task success rate |
| Repo Map reference counting 热点被优化 | Frozen Offline Performance Experiment | `context/repo_map.py`, `evals/repo_map_ablation.py`, frozen report | `python -m evals.verify_evidence_pack` | 同一冻结协议中 median `35.1176s → 0.4928s`，`71.26×`，semantic hash 等价 | 仅是 reference-count 子步骤和该机器/快照；不是 Agent 端到端 71× |
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
- pytest 默认只跑代表性 smoke / contract，不把完整 7×3 正式 benchmark 写回结果目录。

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

## 面试可说 / 不可说

| 能力 | 可以安全说 | 追问证据 | 过度宣称 |
| --- | --- | --- | --- |
| Agent loop | “实现同步 ReAct coding Agent 主循环，并把 finish/completion guard 映射成明确终止状态。” | `agent/core.py`, completion guard tests | “异步高并发 Agent 内核”“真实任务成功率 X%” |
| Tool lifecycle | “统一 validate/pre-hook/permission/tool/post-hook 顺序，并把 policy/tool/infra 失败分层。” | `harness/executor.py`, `tests/test_tool_lifecycle_p0_2.py` | “工具调用无失败”“所有异常都自动恢复” |
| cooperative cancellation | “在 Provider、tool lifecycle 和 step 边界做 cooperative cancel，并保留 Trace 终止语义。” | `agent/core.py`, `harness/executor.py`, failure tests | “可以立即强杀任意同步工具或 Provider 请求” |
| Error Recovery / Failure Harness | “对 transient provider retry、工具失败 Observation、循环/完成性失败和基础设施异常做了确定性故障回归。” | `tests/test_failure_harness*.py` | “Fault tolerance 达到生产级”“故障恢复成功率 X%” |
| Trace v2 | “用 append-only JSONL 记录 run/step/tool/acceptance/delivery correlation，并在落盘边界递归脱敏。” | `agent/event_log.py`, `agent/trace_v2.py`, trace tests | “Trace 可以确定性重放 Agent 执行” |
| Context Compaction | “canonical history 不被覆盖，模型视图做 deterministic pruning + structured compaction，并有 checkpoint lineage。” | implementation + B1/B2 | “B2 证明稳定提升 2 倍成功率”“总结成本为 0” |
| Repo Map | “query-aware ranking 在 12-case commit-history 冻结集上把 MRR 0.097 提到 0.319，预算内 target recall 0.365 提到 0.635。” | formal report/script/fixture | “因此 coding task success rate 提升 X%”“整个 Agent 快 71×” |
| Session | “Chat session 独立持久化 history/round/usage/checkpoint，带 revision conflict、migration、redaction 和 stale pending recovery。” | session store tests | “EventLog 就是 session 数据库”“支持分布式强一致 session” |
| Independent Acceptance | “Runner 在模型不可见的独立阶段执行 path contract / hidden verifier，并把 acceptance 与 Agent status 分开。” | `agent/runner.py`, runner tests | “隐藏 verifier 失败后会自动让 Agent 继续修复” |
| GitHub PR delivery | “实现 Issue→Agent→独立验收→确定性 commit/push/PR，并有 1 个真实 merged PR 案例。” | delivery tests + PR #5 case log | “自动 PR 成功率 100%”“已是生产级 bot”“支持 auto-merge” |

## Resume Claim → Evidence Mapping

| 当前候选主张 | 结论 | 建议表述 | Evidence / 边界 |
| --- | --- | --- | --- |
| ReAct coding Agent | KEEP | “实现同步 ReAct coding Agent 主循环，覆盖 ToolCall、Observation、Reflection 与 completion guard。” | `agent/core.py` + completion tests；不要加总体成功率 |
| 多 Provider abstraction | REWORD | “抽象统一 `LLMBackend`，路由 Anthropic、OpenAI 与 OpenAI-compatible provider/protocol。” | `llm/base.py`, `llm/router.py`；不要说所有 provider 都做过同等 E2E |
| Tool Calling | KEEP | “统一 Tool schema/validation、Hook、Permission、execution 与 Observation 生命周期。” | executor/lifecycle tests |
| Error recovery | REWORD | “实现 transient provider retry、typed failure Observation、loop/completion guard，并用 Failure Harness 做确定性故障回归。” | Failure Harness；不要说生产级容错率 |
| Loop detection | KEEP | “对重复 Action/Observation 指纹与无进展循环做检测和终止/恢复控制。” | `agent/loop_detector.py`, loop tests | 只能写 contract，不写效果百分比 |
| Context compaction | REWORD | “实现 canonical-history-preserving 的 pruning + structured compaction，并用 7-case frozen replay 与 9-run real-model 小样本审计。” | B1/B2；明确小样本边界 |
| Repo Map | KEEP | “query-aware Repo Map 在 12-case frozen commit-history benchmark 上 MRR 0.097→0.319、budget target recall 0.365→0.635。” | 可写数字，但必须带 12-case/frozen 范围 |
| Trace | KEEP | “Trace v2 记录 run/step/tool/acceptance/delivery correlation 与脱敏审计事件。” | Trace tests；不要写 deterministic replay |
| Git Worktree | REWORD | “用 Git Worktree 提供独立 checkout 与成果保留/清理生命周期。” | worktree/orchestrate tests；不能称安全沙箱 |
| Docker isolation | REWORD | “提供 Docker Runtime，默认资源限制与断网，并支持只读根和受控挂载。” | runtime/sandbox tests；不要写“完全安全”，真实 PR case 未覆盖 Docker |
| acceptance verifier | KEEP | “在 Agent history 外执行独立 AcceptanceContract / hidden verifier，再决定 delivery gate。” | runner tests + real PR case |
| GitHub Issue → PR | KEEP | “实现确定性交付 contract，并完成 1 个真实 Issue→merged PR 案例。” | delivery tests + PR #5；不能写总体成功率 |
| benchmark 数字 | KEEP WITH SCOPE | 只使用本页冻结 B1 / Repo Map / B2 数字，并同时写 case/run/protocol 范围 | 不把不同证据层混成一个 Agent 指标 |
| “Agent 总体成功率 X%” | **INSUFFICIENT EVIDENCE** | 不写 | 当前没有足够规模的独立真实任务 benchmark |
| “生产级可靠性 / production ready” | **INSUFFICIENT EVIDENCE** | 不写 | regression coverage ≠ production reliability |
| “自动 PR 成功率 100%” | **INSUFFICIENT EVIDENCE** | 不写 | 只有 1 个真实案例，且同案例经历 4 次调试尝试 |
| “B2 证明 Context Policy 稳定提升成功率” | **INSUFFICIENT EVIDENCE** | 不写 | n=3 cases / variant、每 cell 单次、模型非 deterministic |

## 证据使用规则

任何简历或面试数字必须同时携带它所属的证据层和样本范围。`Implementation Fact ≠ Regression Coverage ≠ Offline Benchmark ≠ Real-model Experiment ≠ Production / Overall Success Rate`。如果未来新增证据，应先冻结 fixture/protocol/result，再更新本索引与只读校验器；不得为了简历表述反向制造 benchmark 数字。
