# Forge Agent 本地协作与交接

本文件是本机 Codex/Agent 的持续交接说明。每次开始任务先读，结束任务前更新“最后交接”。
这是面试项目：优先保证代码可验证、设计可解释、指标不夸大。

## 不可违反

- 保留用户已有修改；先看 `git status` 和 diff，禁止擅自 reset、clean、checkout 覆盖。
- 默认在 `dev` 开发；切分支、pull、rebase、push 前先确认工作区和跟踪分支。
- 修改代码前先明确需求、影响文件、理由和验证计划；若用户已明确授权某一范围可直接实施，则无需逐文件重复确认，但不得越过授权范围。
- 不读取、打印、提交或复制真实 API Key。配置只引用环境变量。
- `config/default.yaml` 按用户配置文件保护，未经明确授权不要修改、提交或还原。
- 私有远程必须使用 SSH 别名：`git@Napabana:Napabana/forge-agent.git`；不要自行改 remote。
- 修复后运行与风险相称的最小测试；不能运行时明确记录原因，不能假称通过。
- 新增或修改代码时补充必要中文注释；能在一行内清晰写完的代码不要无故拆成多行。
- 不为测试结果修改 B1/B2 fixture，不覆盖或重写 `evals/results` 历史结果。
- 每轮完成实际更新后，在 `docs/changes/YYYY-MM-DD/` 新建本轮日志；同一天多轮不同更新分别命名，不覆盖旧日志。
- 每轮最终回复必须给出对应更新日志路径。
- 不处理现有 stash，除非用户明确要求。

## 每次开始

```text
1. git status --short --branch
2. git log -3 --oneline --decorate
3. git remote -v
4. git stash list
5. 阅读本文件“当前状态、下一步、最后交接”
6. 阅读 TODO-P0-P1.md 与 Forge-Agent-P0-P1-实施计划.md
```

## 环境

仓库位置：

- Windows：`E:\2806\forgeAgent\forge-agent`
- WSL：`/mnt/e/2806/forgeAgent/forge-agent`
- 可恢复旧文件备份：`E:\2806\forgeAgent\wsl-recovery-20260913`

Windows venv：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

WSL 使用独立 Linux venv。本机默认发行版固定为 `Ubuntu-22.04-Recovered`；Git 状态与历史检查优先在 Windows 端执行，代码测试可在该 WSL 中执行。

```bash
cd /mnt/e/2806/forgeAgent/forge-agent
source ~/.venvs/forge-agent/bin/activate
python -m pytest -q
```

若 Linux venv 尚未创建，用 `python3 -m venv ~/.venvs/forge-agent`，再安装 `-e ".[dev]"`。Windows venv 不要直接在 WSL 中复用。

`KRILL_API_KEY` 应保存在 Windows 用户环境变量，或 WSL 的 `~/.config/forge-agent/env`（权限 `600`），绝不写真实值到 YAML。

## 项目认知

- `agent/core.py`：同步 ReAct 主循环、完成性守卫、Reflection、循环检测、cooperative cancel 安全边界。
- `agent/runner.py`：CLI/Chat/API/GitHub Issue 的统一 execution composition root，负责 acceptance、isolate 结果归一和统一 post-run Trace。
- `agent/event_log.py`：append-only JSONL 审计与 Trace v2 写盘边界。
- `agent/trace_v2.py`：Trace v2 schema 常量、entrypoint/session 传播和统一 redaction。
- `harness/executor.py`：低层 Tool lifecycle：validation → pre-hook → permission → tool → post-hook；支持显式透明组合。
- `harness/__init__.py`：产品组合根导出的安全默认 ToolExecutor，默认启用 PermissionManager。
- `harness/permission.py`：shell deny/confirm、文件 workspace 边界、deploy confirm 的策略层。
- `llm/`：Anthropic、OpenAI-compatible Chat Completions、OpenAI Responses；provider 错误分类见 `llm/errors.py`。
- `context/`：ConversationHistory、Repo Map、Token Budget、Compaction。
- `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py`：四个产品入口。
- `task/engine.py`：SQLite WAL 任务状态与条件认领。
- `runtime/worktree.py`、`agent/orchestrate.py`：隔离 worktree、成果检测和保留策略。
- `tools/`：具体工具与 runtime 适配。
- `tests/test_failure_harness.py`、`tests/test_failure_harness_isolate.py`：P1-4 默认离线 deterministic failure matrix；fake 只注入故障，不实现第二套 Agent loop。
- `docs/evidence/README.md`：P1-6 统一 Evidence Index，记录 Claim→Evidence→Result→Limitation 与简历/面试表述边界。
- `evals/verify_evidence_pack.py`：P1-6 默认离线只读证据校验入口。

面试表述边界：

- 同步的是 `Agent.run` 内核，asyncio 主要用于编排。
- Worktree 隔离 checkout/index/branch；Docker 隔离进程、网络、资源和根文件系统，但不能表述为“完全安全”。
- cooperative cancellation 不等于能强杀任意正在执行的同步 Tool/provider/callback；已开始的调用在下一安全边界停止。
- PostToolUse 是已发生 Tool 的观察边界；post-hook failure 不覆盖真实 ToolResult。
- 保留 worktree 不等于已经 commit、merge、push 或创建 PR。
- EventLog 是审计记录，不是确定性执行重放。
- Trace v2 是 Forge 自有最小 schema，不是完整 OpenTelemetry implementation。
- 本地 token breakdown 是诊断 estimate；provider usage 才是 provider 返回的真实 usage。
- Failure Harness 证明的是冻结 failure contract 可 deterministic/offline 回归，不是线上可靠性、任务成功率或真实 Provider SLA。
- B1 是 frozen fixture benchmark；B2 v3 只有 9 个真实模型 run；真实 GitHub delivery 当前正式案例只有 1 个。三者都不能外推总体 Agent success rate。
- Repo Map `71.26×` 只对应冻结 reference-count 子步骤性能实验，不是 Agent 端到端提速。

## 当前状态（2026-09-16）

当前唯一状态清单：`TODO-P0-P1.md`。当前实施说明：`Forge-Agent-P0-P1-实施计划.md`。

已完成：

- P0-1：`prepare_next_turn` / shared-history 边界。
- P0-2：Tool Hook / Permission / Cancel 生产语义。
- P0-3：Trace v2 最小闭环。
- P1-1：统一 Runner、独立 acceptance、确定性交付。
- P1-2：Context Compaction C1-C5、B1、B2；C6 延期。
- P1-3：Session 加固。
- P1-4：Failure Harness / deterministic failure injection。
- P1-5：Repo Map 核心能力与正式消融。
- P1-6：Evidence Pack / 面试证据产品化。

**P0/P1 主线已整体 DONE。**

明确延期：完整 Resource Manager、强制终止任意同步 Tool 的通用 async runtime 重写、hidden-verifier feedback、C6 `context_recall(event_ref)`、MCP、多 Agent、multi-tool call、tree-structured session、自动 merge/无人监督发布。

### P0-2 Tool lifecycle 当前事实

- 产品统一顺序：`cancel → validate → pre-hook → cancel → permission → cancel → tool → post-hook → Observation/Trace → cancel`。
- unknown tool / invalid arguments：不进入 Hook、Permission、Tool；返回可恢复 Observation。
- pre-hook block：`HOOK_BLOCKED`；Permission/Tool/post-hook 不执行；Agent 可恢复。
- pre-hook exception：`HOOK_FAILED` fail-closed Observation；Agent 可恢复。
- permission deny / confirm reject：`PERMISSION_DENIED` Observation；Agent 可恢复。
- permission.check / confirm callback 自身 crash：Run `FAILED`，`termination_reason=infrastructure_error`，不是普通 deny。
- normal Tool failure：`TOOL_EXECUTION` Observation；不自动升级 Run FAILED。
- timeout：`ToolErrorType.TIMEOUT` → `ObservationStatus.TIMEOUT`。
- post-hook exception：只写 diagnostic，不覆盖已发生 Tool 的真实结果。
- Tool 已开始后收到 cancel：不强杀同步 Tool；Tool + post-hook 完成、真实结果与 Observation 落 Trace 后，Run 在下一安全边界进入 `CANCELED/canceled`。
- direct 产品 Runner 默认启用 PermissionManager；isolate 继续绑定 `PermissionManager(workspace=<worktree>)`。
- 四入口均通过 Runner/Agent/ToolExecutor，不直接执行 Tool。
- GitHub Issue 自动 PR 模式仍不向 Agent 开放 `git_add/git_commit`。
- P0-2 没有新增大量 Hook event；继续复用 Trace v2 permission/tool/run 事件与写盘级 redaction。

完整说明：`docs/changes/2026-09-16/P0-2-Tool-Hook-Permission-Cancel收口改动内容.md`。

### P0-3 Trace v2 当前事实

- 新写入 EventLog 统一 `trace_schema_version=2`，保留 `schema_version=2` 兼容别名。
- Run 使用 `run_id/run_span_id`；Model/Tool/Context 使用 child span 与 operation id。
- Runner 统一追加 acceptance 与 `run_terminated`；GitHub Issue 在 commit/push/PR 结果后追加 delivery。
- CLI/Chat/API/GitHub Issue 通过统一 Runner 解析 entrypoint；isolate 路径用轻量 ContextVar 传播到深层 EventLog 创建点。
- EventLog 最终 JSONL 写盘边界递归 redaction；usage token 字段显式保护。
- Model span 记录 `token_breakdown` 与独立 `provider_usage`。
- provider error、infrastructure error、cancel、loop detected、resource exhausted、completion rejection、acceptance、delivery 可在同一 run correlation 下审计。
- 旧 JSONL 可直接 replay；历史行不迁移不重写，新 append 才使用 v2 metadata。
- B1/B2 历史结果和 fixture 未改写。

完整说明：`docs/changes/2026-09-16/Trace-v2收口改动内容.md`。

### P1-4 Failure Harness 当前事实

- composition root 仍是 `ExecutionRunner`；实际生命周期仍是 `ExecutionRunner → Agent → ToolExecutor → EventLog/Trace v2 → RunResult`。
- test-local `ScriptedFailureBackend` 只提供固定 return/raise 序列，不调用真实 Provider。
- Provider：connection/timeout retry、retry exhausted、non-retryable/parser exception、retry wait cancel 已有统一 Runner 回归。
- Hook：pre-hook block/exception 可恢复；post-hook exception 不覆盖真实 Tool outcome。
- Permission：deny/confirm reject 可恢复；permission subsystem/confirm callback crash 为 fatal infrastructure。
- Tool/runtime：unknown、invalid args、normal failure、execute exception、timeout、unresolved/repeated fatal runtime 均可 deterministic 注入。
- Cancel：用 Event-like fake / `threading.Event` 固定安全边界；已经开始的同步操作不伪装成强制中断。
- Context：shared-history 首轮 prepare 现在直接复用 `Agent._prepare_next_turn`，与 step>1 的 exception/cancel/Trace 语义一致。
- Completion/termination：completion rejection recover、max_steps/resource_exhausted、loop_detected、GAVE_UP、FAILED、CANCELED 均在 failure matrix 或既有 B2/P0-2 回归固定。
- Acceptance：Agent 非成功时 verifier skipped；Agent SUCCESS + acceptance fail 保持两层状态。
- Delivery：失败场景不触真实 push/PR；acceptance fail 在 git side effect 前 blocked。
- Trace：failure event + `run_terminated`、run/model correlation、entrypoint、failure-path redaction 有回归；没有 Trace v3。
- isolate sandbox preflight 使用 fake DockerRuntime/FakeWorktreeSession，不依赖本机 Docker；Runner 归一为 `FAILED/infrastructure_error`。
- 默认日常命令：`pytest tests/test_failure_harness*.py -q`。
- malformed/empty response 在当前 `LLMBackend` contract 中按 parser exception 注入；不新增返回任意坏对象的 Backend API。

完整说明：`docs/changes/2026-09-16/P1-4-Failure-Harness收口改动内容.md`。

### P1-6 Evidence Pack 当前事实

- Evidence Index：`docs/evidence/README.md`。
- 默认只读离线入口：`python -m evals.verify_evidence_pack`。
- Evidence 分类：Implementation Fact / Deterministic Offline Regression / Frozen Offline Benchmark / Real-model Small Sample / Real End-to-End Case。
- Context B1：7 cases × 3 variants = 21 frozen rows；hybrid 7/7，只代表 fixture benchmark。
- Repo Map：12-case commit-history benchmark；MRR 0.097→0.319，预算内 target recall 0.365→0.635；reference-count 71.26× 仅代表该子步骤。
- B2 v3：`deepseek-v4.1-flash`，3 cases × 3 variants × 1 run = 9 real-model runs；非 deterministic，无 repeat/seed。
- GitHub delivery：当前正式可引用真实案例为 1 个 Issue→merged PR；不能计算或宣称总体自动 PR 成功率。
- Resume Claim → Evidence Mapping 已落文档；无证据主张标记 `INSUFFICIENT EVIDENCE`。

完整说明：`docs/evidence/README.md`、`docs/changes/2026-09-16/P1-6-Evidence-Pack改动内容.md`。

### 当前分支与远程事实

- 远程工作分支：`dev`。
- P1-6 Evidence Pack 实现提交：`915850016cd43cdc289732540db04628eacd6794`；后续测试契约修正已继续推进 `dev`，最终交接始终以当前最新 HEAD 为准。
- 用户已在本地完成 P1-6 要求的 Evidence Pack、Failure Harness、Trace/Runner、benchmark reader 与全量 pytest，并确认全部通过。
- `config/default.yaml` 未修改。
- B1/B2 fixture 未修改，`evals/results` 未重写。
- GitHub connector 当前仍不能直接执行用户本机 pytest；本轮 DONE 判断基于用户明确提供的本地验证结果，不虚构测试数量或耗时。

## 已知问题与下一步

P0/P1 主线已整体收口，不再把“继续加功能”作为默认下一步。

下一阶段优先：

1. 简历与面试直接使用 `docs/evidence/README.md` 的 Claim→Evidence Mapping；
2. 只保留可回链到实现、测试、冻结 benchmark 或真实案例的技术主张；
3. 不把一次 PR、fixture pass rate、单元测试数量或 B2 `n=3` 包装成总体成功率；
4. 只有真实使用或新 benchmark 暴露明确缺口时，才 reopen 对应 P0/P1 条目；
5. Repo Map cache identity / shell-git stale-map 等保持条件执行尾项，不阻塞当前阶段。

不要为了简历堆功能。每个新增主张必须能指向实现、测试或可复现实验；测试覆盖率不等于 Agent 真实任务成功率。

## P0/P1 收口验证

默认离线证据校验：

```bash
python -m evals.verify_evidence_pack
```

核心 closure regression：

```bash
pytest -q \
  tests/test_evidence_pack.py \
  tests/test_failure_harness.py \
  tests/test_failure_harness_isolate.py \
  tests/test_trace_v2.py \
  tests/test_runner.py \
  tests/test_context_policy_benchmark.py \
  tests/test_repo_map_ablation.py

pytest -q
```

用户已确认上述要求的验证全部通过。未来若失败：保留原始失败输出，不修改 fixture 或历史 result；按失败节点重新打开对应阶段。

## 每次结束：更新最后交接

必须在本文件末尾覆盖更新以下内容：

- 本轮目标和结论。
- 实际修改的文件。
- 测试命令与真实结果。
- 未提交修改、stash、分支和 remote 状态。
- 明确的下一步或阻塞原因。

### 最后交接（2026-09-16，P1-6 Evidence Pack / P0-P1 阶段收口）

- P1-6 Evidence Pack 已完成：统一 Evidence Index、只读离线校验入口、Evidence taxonomy、Resume Claim→Evidence Mapping 和“可说/不可说”边界均已落地。
- 用户已在本地执行 P1-6 要求的 Evidence Pack 自测、Failure Harness、Trace/Runner、Context/Repo Map benchmark reader 与全量 pytest，并明确确认全部通过；未提供具体 passed 数量或耗时，因此文档不补写数字。
- `TODO-P0-P1.md`、`Forge-Agent-P0-P1-实施计划.md`、`AGENTS.md` 已统一将 P1-6 标为 `DONE`；P0-1～P0-3、P1-1～P1-6 整体收口。
- 本轮只更新状态/交接文档与 closure changelog；不修改 Agent runtime、B1/B2 fixture、`evals/results` 或 `config/default.yaml`。
- P0/P1 后续默认不继续扩 MCP、Multi-Agent、parallel tools、Resource Manager 或新的 Context 策略；只有真实证据显示必要时才 reopen。
- 简历与面试证据统一以 `docs/evidence/README.md` 为入口。Repo Map 12-case benchmark、B1 frozen fixture、B2 9-run small sample、1 个真实 merged PR 必须保持各自证据边界。
- 当前 connector 只能确认远端 `dev`，无法读取用户本地未提交修改或 stash，因此不对本地工作区状态做额外断言。
- 本轮更新日志：`docs/changes/2026-09-16/P1-6-Evidence-Pack收口-DONE.md`。

### 最后交接（2026-09-17，Model-aware Token Budget / Context Compaction 收口）

- 本轮将生产 Context Budget 从固定 80k / 固定 15% reserve 收口为 Model Capability + Forge request policy：`effective_window - request_output_reserve - safety_margin`；旧 `budget_tokens=80000` 只作为 Forge context cap fallback，不再声称是模型 Context Window。
- 新增 `llm/capabilities.py::ModelCapabilities`；Router 向 Backend 暴露 context window、model max output、request max output、Forge cap、safety margin 与 semantic packet cap。未知 OpenAI-compatible proxy 不硬编码模型能力。
- `llm.max_tokens` 兼容读取为 request `max_output_tokens`；新增 `context_window`、`model_max_output_tokens`、`max_output_tokens`、`context_budget_cap`、`context_safety_margin_tokens`、`semantic_packet_max_tokens` 语义。CLI `--model` 变化时旧 capability 失效，只保留 Forge cap fallback。
- `TokenCounter` 新增 model-aware tiktoken 路径与 conservative local estimator；pre-request estimate 与 provider-reported `TokenUsage` 保持独立，后者仍是请求后 accounting / Trace truth。
- Semantic Packet 已从字符预算改为 token budget；最近 user-authored evidence 优先选择，选中后恢复时间顺序，最新消息过大时保留 bounded prefix 而不是让旧冲突指令占位。
- `context/compaction.py` 的 pressure、recent-tail、before/after accounting 复用同一 TokenCounter；Stage A deterministic pruning 策略本身未重做。
- 新增 `tests/test_model_aware_token_budget.py`，固定 32k/128k pressure、output reserve、context cap、mixed packet bound、recent override、provider usage、legacy config migration 与 backend capability 边界。
- 当前 ChatGPT 执行容器无法解析 `github.com`，无法 clone 远端仓库执行仓库级 pytest；本轮没有虚构 pytest 通过。实际只运行了完全离线纯函数行为校验，六个核心行为均通过。用户本地应优先执行新增测试、现有 TokenBudget/Context 测试，再视成本跑全量 pytest。
- `config/default.yaml`、P2 Repo Map、B1/B2 fixture、`evals/results` 均未修改。connector 无法读取用户本地未提交修改或 stash，因此不对本地工作区状态做断言。
- 本轮更新日志：`docs/changes/2026-09-17/Model-Aware-Token-Budget收口.md`。

### 验证补充（2026-09-17，Model-aware Token Budget 本地回归）

- 上一条交接中“ChatGPT 执行容器无法运行仓库级 pytest”保留为实现提交当时的真实状态，不回写历史。
- 用户随后在本地执行 `tests/test_model_aware_token_budget.py`、既有 TokenBudget/Context Compaction 相关回归，以及 `python -m pytest -q` 全量测试，并明确确认三层验证全部通过。
- 用户未提供 passed 数量、完整 stdout 或耗时，因此这里只记录“全部通过”，不补造数字。
- 本轮仅补验证证据，不修改 Agent runtime、Context/LLM 实现、测试 fixture、B1/B2 或 `evals/results`。
- GitHub connector 只能确认远端 `dev`，无法读取用户本地未提交修改或 stash，因此仍不对本地工作区状态做额外断言。
- 本轮更新日志：`docs/changes/2026-09-17/Model-Aware-Token-Budget本地回归验证收口.md`。
