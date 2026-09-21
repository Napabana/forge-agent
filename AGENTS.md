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
- `harness/permission.py`：shell deny/confirm、文件 workspace 边界、MCP capability effect/confirm 的策略层。
- `llm/`：Anthropic、OpenAI-compatible Chat Completions、OpenAI Responses；provider 错误分类见 `llm/errors.py`。
- `context/`：ConversationHistory、Repo Map、Token Budget、Compaction。
- `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py`：四个产品入口。
- `task/engine.py`：SQLite WAL 任务状态与条件认领。
- `runtime/worktree.py`、`agent/orchestrate.py`：隔离 worktree、成果检测和保留策略。
- `tools/`：具体工具与 runtime 适配。
- `mcp_integration/`：P2-4 MCP Host client bridge；official SDK connection lifecycle、remote Tool adapter 与 ToolRegistry registration。
- `experience/`：P2-5 offline trajectory/experience/candidate/evaluation/promotion pipeline；不进入 Agent 主循环，正式生效仍由 P2-3 SkillCatalog/SkillRuntime 负责。
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

明确延期：完整 Resource Manager、强制终止任意同步 Tool 的通用 async runtime 重写、hidden-verifier feedback、C6 `context_recall(event_ref)`、多 Agent、multi-tool call、tree-structured session、自动 merge/无人监督发布。

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

### 最后交接（2026-09-17，Smoke Test 配置与依赖修复）

- 修复 `smoke_test.py` 直接按系统默认编码读取 YAML 导致的 Windows `UnicodeDecodeError`。
- 冒烟脚本现在复用 `config.schema.load_config`，可读取正式配置和项目 env 文件，再兼容传给 `create_backend_from_config`。
- 当前项目 `.venv` 已安装项目声明的 `openai>=1.30.0`；backend 实例化验证通过，未发起真实 API 请求。
- 本轮修改：`smoke_test.py`、`docs/changes/2026-09-17/smoke-test配置与依赖修复.md`。
- 验证：`.venv\\Scripts\\python.exe -m py_compile smoke_test.py`、配置加载和 `OpenAICompatBackend` 实例化均通过。
- 当前分支为 `dev`，工作区有本轮未提交修改；未处理 stash，未修改 `config/default.yaml`、fixture 或历史结果。
- 下一步使用项目解释器运行：`.\\.venv\\Scripts\\python.exe smoke_test.py`；本轮没有虚构真实 API 请求成功。

### 最后交接（2026-09-17，ContextBudgetSpec EventLog 序列化修复）

- 修复 `ContextBudgetSpec` 被 `dataclasses.asdict()` 深拷贝时因 `__new__` 必填元数据缺失而崩溃的问题。
- 修复位于 `config/schema.py` 的类型边界，序列化时转换为普通 `int`，覆盖 CLI、Chat、API、GitHub Issue 和 smoke_test 的共同 Task 路径。
- 新增 `tests/test_model_aware_token_budget.py` 的 Task 序列化回归；专项测试 9 passed。
- 本轮修改：`config/schema.py`、`tests/test_model_aware_token_budget.py`、`docs/changes/2026-09-17/smoke-test配置与依赖修复.md`、本交接记录。
- 已通过 `.venv\\Scripts\\python.exe -m pytest tests/test_model_aware_token_budget.py -q` 和相关文件 `py_compile`。
- 未运行真实 LLM 请求；下一步可用 `.\\.venv\\Scripts\\python.exe smoke_test.py` 验证 API 联通和工具执行。

### 最后交接（2026-09-18，Chat / Direct 默认 CWD 统一修复）

- 修复 Chat/direct target repo 与 Shell/Test/Git `default_cwd` 未统一绑定的问题；File tools workspace 行为保持不变。
- `_build_registry` 已用独立 `default_cwd` 替代含混的 `worktree_path` 参数，`workspace` 不再隐式回退到 worktree。
- CLI direct、Chat、GitHub Issue 显式绑定目标 repo；isolate 显式绑定当前 worktree；三个 eval 和 demo/test builder 已同步新契约。
- 新增 registry 参数独立性、CLI direct、Chat 和 isolate/GitHub 接线回归；LLM 显式 `cwd` 覆盖默认值的既有语义未改变。
- Codex 环境的系统 Python 与随附 Python 均缺少 pytest；用户随后在本地执行本轮测试并明确确认全部通过，未提供 passed 数量或耗时，因此不补造数字。
- 本轮 13 个相关 Python 文件的静态编译检查通过；`git diff --check` 仅报告既有 LF/CRLF 提示。
- 工作区原有 Smoke Test / Model-aware Token Budget 修改、`config/default.yaml` 修改及 stash 均未覆盖或处理；当前分支仍为 `dev`。
- 本轮更新日志：`docs/changes/2026-09-18/Chat-Direct默认CWD统一修复.md`。

### 最后交接（2026-09-18，GitHub Issue Live Progress / 共享 Event Renderer）

- 新增 `entry/event_renderer.py::RunEventRenderer`，CLI、Chat、GitHub Issue 统一消费 EventLog event，不再各自维护 step/tool/observation 打印逻辑。
- CLI direct 改为实时 `on_event`，Chat 使用 per-session renderer；GitHub Issue 的 Runner、acceptance 和 delivery 追加阶段复用同一个 observer。
- `ExecutionRunner` / `orchestrate_run` 已贯通 isolate `on_event`；既有 AgentBus 转发与 UI observer 共用一次 EventLog append，不新增执行或日志状态机。
- 统一实时展示 step、action、tool、observation status、acceptance、delivery，并保留任务终态、Reflection、Context Compaction 标记。
- CLI run、Chat、GitHub Issue 新增独立 `--reasoning-stream/--no-reasoning-stream`；CLI/Chat 未显式设置时兼容跟随 `--stream`，GitHub Issue 默认关闭 reasoning，生命周期进度始终显示。
- 回归：入口/renderer 定向 104 passed；Trace/lifecycle/stream 123 passed；reasoning 契约复跑 40 passed。
- WSL 全量 pytest：822 passed、14 skipped、2 个 AnyIO/Python 3.11 deprecation warnings，118.33s；13 个相关 Python 文件静态编译通过。
- 用户原有 `config/default.yaml` 修改、B1/B2 fixture、`evals/results` 与 stash 均未处理；当前分支仍为 `dev`。
- 本轮更新日志：`docs/changes/2026-09-18/GitHub-Issue-Live-Progress共享EventRenderer.md`。

### 最后交接（2026-09-18，Docker E2E 执行语义收口）

- 基于真实 `--isolate --sandbox --confirm` E2E 暴露的问题，重新打开三项 correctness 收口；实现基线为 `dev@084426470d7e79da0d1d6db8ec6d31cb70c55bb8`。
- Completion Guard 改为 repository-state-driven：工具后统一比较 `repository_fingerprint`，记录 `last_repo_change_step`；FINISH 用 final vs initial repository state 判断 required changes，并用最后成功测试 step 对比最后真实 repo change step。shell/git/未来真实修改工具不再依赖工具名白名单；同一 fingerprint 同时喂给 loop detector，未知路径变更触发增量 Repo Map 下一轮 sync。
- 生产确认单一权威收口为 `ToolExecutor + PermissionManager`：`entry/cli.py::_build_registry` 不再向 `ShellTool` 注入交互 confirm callback；`ShellTool` standalone confirmation API 保留。CLI/Chat 的 callback 继续只注入 Runner/ToolExecutor，isolate 继续由 orchestrator 的 workspace-bound PermissionManager 控制。
- Sandbox 路径语义拆分：宿主 `task.repo_path/worktree` 继续服务 file tools、PermissionManager、Repo Map、repository fingerprint 与 worktree 生命周期；模型可见 execution workspace 在 sandbox 下为 `/workspace`，prompt 明确 shell 已在该 cwd 且应优先相对路径。未重写 shell command，也未把内部 Task.repo_path 替成容器路径。
- `test` 工具 schema 已明确为 pytest 工具，本轮未为偶发 `run_tests` 幻觉引入 alias system。
- 新增/更新 deterministic regression，覆盖 shell 真修改、测试后 shell 再修改、最终恢复 initial state、无真实变化、file_write/HEAD commit、生产 shell 单次 confirm yes/no/callback crash、sandbox model-visible workspace 与 host workspace 分离。既有 `tests/test_sandbox.py` 继续固定 Docker cwd host→`/workspace` 映射；`tests/test_confirm.py` 继续固定 standalone ShellTool contract。
- 当前 ChatGPT 环境无法获得可执行的仓库 checkout，因此本轮未真实运行 pytest，也不声明通过；用户本地应先跑定向回归，再跑 `pytest -q` 与真实 Docker E2E。
- 未修改 `config/default.yaml`、B1/B2 fixture、`evals/results` 或 Independent Acceptance 语义。GitHub connector 无法读取用户本地未提交修改/stash，因此不对本地工作区状态作断言。
- 本轮更新日志：`docs/changes/2026-09-18/Docker-E2E执行语义收口.md`。



### 最后交接（2026-09-18，P2-0 Coding Agent Evaluation Harness）

- P2-0 已完成实现，状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现基线为 `dev@a9c745f77db4adae4818f381261a2dd4c151e552`。
- 新增 `evals/coding_agent/` 通用 task-level Evaluation Harness：Evaluation 层负责 suite/case/trial/environment/grader/artifact/aggregation，Agent 层继续复用 `ExecutionRunner → Agent → ToolExecutor`。
- 首版 `evals/fixtures/coding_agent/suite.json` 固定 8 个 coding task，并带 reference solution；hidden deterministic grader 不进入 Agent prompt。
- Grader 支持 command/file/repository_state/run_trace；TrialResult 保存 RunStatus、termination、acceptance、steps/tokens/provider usage/wall time、tool/test/completion-rejection/reflection metrics、patch/final-state/trace refs。
- fake/scripted backend 只证明 Harness correctness，聚合 report 故意不生成 capability pass rate；真实模型必须显式 `--real-model`。
- 本轮没有真实模型 baseline；`evals/results/coding_agent_baseline_not_executed/` 明确记录 `execution_status=not_executed`、`real_model_executed=false`。
- 输出目录默认拒绝静默覆盖；variant/repetition/trial id 已为 P2-1～P2-5 architecture A/B 留出统一扩展点。
- 新增 `tests/test_coding_agent_eval.py` 覆盖 schema、duplicate id、stable trial id、clean fixture、grader、Agent failure result、metrics、report、no-overwrite、not-executed、fake evidence boundary 与 reference self-check。
- 当前 ChatGPT 环境没有可执行仓库 checkout，因此未真实运行 pytest，不声明测试通过；本地应先执行 `python -m pytest -q tests/test_coding_agent_eval.py`，再跑 Runner/Failure Harness/Trace/Acceptance/Evidence Pack 回归和全量 `python -m pytest -q`。
- 本轮未实现 Planning、RecoveryPolicy、Skills、MCP、Evolution、Multi-Agent 或 LLM-as-Judge 主链。
- 更新日志：`docs/changes/2026-09-18/P2-0-Coding-Agent-Evaluation-Harness.md`。

### 验证补充（2026-09-18，P2-0 Coding Agent Evaluation Harness 本地回归）

- P2-0 实现提交：`36aa0895730ca4145947e3bee4f35779dc5563ae`（`feat: add coding agent evaluation harness`）。
- 用户随后在本地执行 P2-0 新增专项测试、相关 Runner / Failure Harness / Trace / Acceptance / Evidence Pack 回归，并执行全量 pytest；用户明确确认全部通过。
- 用户未提供具体 passed 数量、完整 stdout 或耗时，因此本交接只记录“全部通过”，不补造数字。
- P2-0 状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 收口为 `DONE`。
- 该验证只证明 Evaluation Harness 的 deterministic contract 与现有生产执行主链兼容；没有执行真实模型 baseline，不产生 Coding Agent success rate、token、latency 或 pass@1 结论。
- `evals/results/coding_agent_baseline_not_executed/` 继续保持 `execution_status=not_executed`、`real_model_executed=false`，不因离线 pytest 通过而改写。
- 下一阶段进入 P2-1 Structured Planning，并继续以 P2-0 的 `baseline_react` / variant / repetition 协议作为统一 A/B 入口。
- 本轮验证补充日志：`docs/changes/2026-09-18/P2-0-Coding-Agent-Evaluation-Harness本地回归-DONE.md`。


### 最后交接（2026-09-19，P2-1 Structured Planning）

- P2-1 已完成代码实现，当前状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现基线为 `dev@60e1194d50f9d23c64a57f6e90d51f6842319bfd`。
- 新增 `agent/planning.py`：typed `ExecutionPlan / PlanStep / PlanRevision` 与 `planning_mode=off|auto|always`；没有新增第二套 Agent loop、Planner Agent 或 Provider 专属 plan parser。
- Planning 复用既有 Function Calling ToolCall 结构，通过 `plan_create / plan_step_update / plan_revise` 三个 internal control 更新 runtime state；真正 repository Tool 继续走 `ToolExecutor → Hook → Permission → Tool → post-hook`。
- `always` 允许显式 read-only exploration，但 mutation/FINISH 前必须有有效 plan；`auto` 使用 deterministic complexity signals 并记录 decision reason；`off` 不增加 plan schema 或额外 model call。
- Tool abstraction 增加保守 effect metadata：未知 Tool 默认 may-mutate；file read/view、search、git status/diff、test 显式 read-only；shell 保守按 may-mutate。
- Current plan 每轮作为 bounded runtime system context 注入，不重复进入 canonical ConversationHistory；因此 Context Compaction/HistoryWindow 不负责保存 current plan。token breakdown 增加 diagnostic `planning_tokens`，provider usage 仍是权威 accounting。
- Plan progress/revision 只接受显式 structured update，不把 Tool success 自动标成 PlanStep 完成；Completion Guard/Acceptance 权威语义未改变。
- Trace v2 增加 `plan_created / plan_step_started / plan_step_completed / plan_revised / planning_skipped / plan_rejected`；没有 Trace v3。
- P2-0 Eval CLI 正式映射 `baseline_react → planning_mode=off`、`planning → planning_mode=always`；TrialMetrics 增加 plan created/revision/completed/skipped 指标。共享 8-case outcome grader 未加入 plan-only 约束。
- 新增 `tests/test_structured_planning.py` 并扩展 `tests/test_coding_agent_eval.py`；当前 ChatGPT 环境没有可执行仓库 checkout，未真实运行 pytest，因此不声明通过。
- 本轮没有 real-model A/B，没有 success-rate/token/latency improvement 数字；Evidence Pack 暂不新增“Regression passed” claim，待用户本地验证后再收口。
- 更新日志：`docs/changes/2026-09-19/P2-1-Structured-Planning.md`。


### 验证补充（2026-09-19，P2-1 Structured Planning 本地回归）

- 用户在 P2-1 实现后完成多轮定向与全量本地 regression；过程中暴露的 Trace token accounting、empty planning token attribution、PyYAML `off` 解析问题均已修复。
- 用户最终再次执行全量 `python -m pytest -q` 并明确确认全部通过；最终通过轮次未提供具体 passed 数量、完整 stdout 或耗时，因此不补造数字。
- P2-1 状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 正式收口为 `DONE`。
- deterministic evidence 覆盖 typed Plan/Step/Revision、off/auto/always、compaction-surviving current plan、Trace v2 planning events/token diagnostics、产品入口配置、P2-0 planning variant 与既有 Runner/Completion Guard/Failure Harness 回归兼容。
- 本轮没有执行 real-model `baseline_react vs planning` A/B，不产生 success-rate、pass@1、token、latency 或 Planning 提升结论。
- 下一阶段进入 P2-2 Failure-aware Recovery + Replanning，并直接复用 P2-1 current plan / current step / plan revision mechanism。
- 验证日志：`docs/changes/2026-09-19/P2-1-Structured-Planning本地回归-DONE.md`。


### 最后交接（2026-09-19，P2-2 Failure-aware Recovery + Replanning）

- P2-2 已完成首版代码实现，当前状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现基线为 `dev@1ad7850b8e84ebad650ccc3f16d46c791368f14a`。
- 新增 `agent/recovery.py`：typed `FailureContext / RecoveryDecision / RecoveryPolicy / RecoveryRuntime`；继续复用唯一的 `ExecutionRunner → Agent → ToolExecutor` 主链。
- `recovery_mode=off|structured`，默认 `off` 保持旧行为；`recovery_max_attempts` 提供 bounded recovery budget。
- structured mode 分类 Tool/Test failure、Permission denied、Loop、No Progress 与 Completion rejected；Provider retry、cancel、prepare/context infrastructure 和 fatal runtime infrastructure 保持既有权威语义，不重复实现。
- P2-1 Replanning 已做强约束：Recovery 选择 REPLAN 且已有 current plan 时，记录当前 plan version；在 `plan_revise` 产生新版本前，mutation 与 FINISH 被 `RECOVERY_BLOCKED` gate，read-only diagnosis 允许。
- unresolved replan gate 每轮作为 runtime system context 注入，避免 history trimming/compaction 丢失 recovery state；没有建立第二套 Recovery memory。
- Trace v2 增加 `failure_classified / recovery_selected / recovery_exhausted / recovery_blocked`；P2-0 Eval 新增 `planning_recovery` variant 与 recovery metrics。
- 新增 `tests/test_structured_recovery.py`，并扩展 `tests/test_failure_harness.py`、`tests/test_day6.py`、`tests/test_coding_agent_eval.py`，覆盖 recovery off compatibility、test failure→replan、replan gate、completion rejection→retest、budget exhaustion、provider retry separation、permission deny 与 infra boundary。
- 当前 ChatGPT 环境没有可执行仓库 checkout，因此尚未运行 pytest；不要把本状态写成 DONE，也不要声明 real-model recovery 收益。
- 本轮更新日志：`docs/changes/2026-09-19/P2-2-Failure-aware-Recovery-Replanning.md`。

### 验证补充（2026-09-19，P2-2 Failure-aware Recovery + Replanning 本地回归）

- 用户完成 P2-2 新增专项、关键兼容、全量 pytest 和 Evidence Pack 校验，并明确确认全部通过。
- 用户未提供最终通过轮次的具体 passed 数量、完整 stdout 或耗时，因此不补造数字。
- P2-2 状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 正式收口为 `DONE`。
- deterministic evidence 覆盖 typed FailureContext/RecoveryDecision、bounded RecoveryPolicy、replan runtime gate、compaction-surviving recovery state、Trace v2 recovery events、P2-0 `planning_recovery` variant，以及 Provider retry / cancel / infrastructure contract 不回归。
- 本轮没有执行 real-model `planning vs planning_recovery` A/B，不产生 success-rate、pass@1、token、latency 或 recovery effectiveness 数字。
- 下一阶段进入 P2-3 Agent Skills；Skill 必须作为 workflow/context capability 接入现有 Agent 与 ToolExecutor，不得绕过 Permission/Hook/Cancel/Trace。
- 验证日志：`docs/changes/2026-09-19/P2-2-Failure-aware-Recovery-Replanning本地回归-DONE.md`。

### 最后交接（2026-09-19，P2-3 Agent Skills）

- P2-3 已完成首版代码实现，当前状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现基线为 P2-2 DONE 后的当前 dev。
- 新增 `skills/catalog.py` / `skills/runtime.py`：扫描 `<repo>/.agents/skills/` 与 global root，校验 `SKILL.md` frontmatter，project 同名 Skill 覆盖 global，单个坏 Skill 只记 discovery issue，不阻断 Agent。
- progressive disclosure：system 首轮只包含 metadata；模型显式 `skill_load` 后才加载完整 instructions；已加载 Skill 的 reference 可通过 `skill_reference_load` 再按需进入 runtime context。
- SkillRuntime 与 Planning/Recovery 一样是 per-run runtime state；loaded Skill/reference 每轮重新注入 system context，不依赖 canonical history，因此 compaction/history override 不会丢当前 Skill state。
- Skill scripts 只暴露 manifest，Skill subsystem 不执行脚本；任何 executable action 仍必须使用现有 Tool/Shell，经 ToolExecutor/Permission/Hook/Cancel/Trace。
- 新增 Trace v2 Skill events：`skill_discovered / skill_selected / skill_loaded / skill_reference_loaded / skill_rejected`。
- 正式 config 增加 `skills_enabled`、global dir、loaded/context 上限；默认关闭并接入 CLI/Chat/API/GitHub Issue。
- P2-0 Eval 新增 `planning_recovery_skills` variant，固定使用 eval fixture Skills；`skill_selection` 是 required=false 的 process grader，不参与 coding task success/acceptance。
- `pyproject.toml` 已加入 `skills*` package discovery，避免源码测试通过但安装包漏掉新 package。
- 新增 `tests/test_agent_skills.py`，并扩展 `tests/test_coding_agent_eval.py`、`tests/test_day6.py`。当前 ChatGPT 环境未运行 pytest，不得写 DONE，也不得宣称 Skill 提升成功率/token/latency。
- 更新日志：`docs/changes/2026-09-19/P2-3-Agent-Skills.md`。

### 验证补充（2026-09-19，P2-3 Agent Skills 本地回归）

- 用户完成 P2-3 新增专项、关键兼容、package discovery、not-executed Eval 接线、Evidence Pack 与全量 pytest，并明确确认全部通过。
- 用户未提供最终通过轮次的具体 passed 数量、完整 stdout 或耗时，因此不补造数字。
- P2-3 状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 正式收口为 `DONE`。
- deterministic evidence 覆盖 metadata-only discovery、progressive disclosure、reference on-demand、project-over-global、malformed Skill isolation、compaction-surviving SkillRuntime、script non-execution、Trace Skill events、产品入口配置、package discovery 与 P2-0 `planning_recovery_skills` process evaluation。
- 本轮没有执行 real-model `planning_recovery vs planning_recovery_skills` A/B，不产生 success-rate、pass@1、trigger accuracy、token、latency 或 Skill effectiveness 数字。
- 下一阶段进入 P2-4 MCP Client / Tool Adapter；MCP 只作为 capability source，真实 invocation 必须适配成 Forge Tool 并继续经过 ToolExecutor/Permission/Hook/Cancel/Trace。
- 验证日志：`docs/changes/2026-09-19/P2-3-Agent-Skills本地回归-DONE.md`。


### 最后交接（2026-09-19，P2-4 MCP Client / Tool Adapter）

- P2-4 已完成首版代码实现，当前状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现起点为 `dev@1a3e72318ba2df1722a0dede0ec55ca916282abd`，最终交接以当前 dev 最新 HEAD 为准。
- 新增 `mcp_integration/manager.py` / `adapter.py` / `registry.py`，基于 official MCP Python SDK v2；没有手写 JSON-RPC，也没有新增第二套 Agent loop。
- 正式调用链保持 `LLM → ToolRegistry → ToolExecutor → Hook → Permission → MCPToolAdapter → MCPClientManager → official SDK`；MCP 不绕过现有 Permission / Hook / cooperative cancel / Planning mutation gate / Trace。
- `MCPClientManager` 用专用 asyncio loop thread 长期持有 official `Client` context；同步 Forge Tool 通过 thread-safe future 调 async client，不为每次 ToolCall 重建连接。direct/Chat 跨 run 复用 manager；isolate/worktree 每次 run 创建独立 manager；CLI/API/GitHub/Eval 都显式 cleanup。
- 首版支持 stdio；Streamable HTTP 因 v2 `Client(URL)` 与 stdio 共用同一 lifecycle abstraction，以薄 transport 分支一并接入；旧 SSE 不作为新主线。
- remote Tool 名统一 namespaced 为 `mcp__<server_id>__<tool>`；schema/collision/output/error mapping 有确定性规则。schema/description/tool-count 分别有 32k/2k/128 硬上限，避免不可信 capability metadata 无界占用。
- 新增 `ToolErrorType.REMOTE_CAPABILITY`：stdio startup/discovery、disconnect/protocol/server failure 都属于 remote capability failure，可由 P2-2 当普通 Tool failure 消费；只有 Forge manager/lifecycle invariant 才是 `INFRASTRUCTURE`。
- ToolAnnotations 默认不可信。server 只有显式 `trust_read_only_annotations=true` 时才允许 `read_only_hint=true` 映射 `READ_ONLY`；否则保守 `MAY_MUTATE_REPOSITORY → CONFIRM`。MCP CONFIRM 会向用户展示 namespaced tool + bounded 参数预览。
- Trace v2 增加 `mcp_server_capabilities / mcp_tool_discovered`，并在 tool lifecycle event 记录 server id / remote tool / transport / safety-hint correlation；不记录 URL/env/secret。
- Eval 新增 `planning_recovery_skills_mcp` variant，固定使用仓库内 local stdio fixture，不读取用户随机 MCP 配置；TrialMetrics 增加 MCP discovery/call/failure counts。另新增独立 `evals/fixtures/coding_agent/mcp_suite.json`，不修改 P2-0 冻结的 8-case suite，并用 required_tool grader 锁定“实际调用 MCP guidance”。
- `tests/test_mcp_integration.py` 覆盖 official SDK in-process、local stdio Host E2E、tools/resources/prompts capability negotiation、multi-server、startup failure、server crash、schema/metadata bounds、permission、hooks、cancel、direct reuse、isolate ownership、Planning、Recovery、Eval mapping/package discovery。
- ChatGPT 执行容器仍无法解析 `github.com`，GitHub connector 也没有启动新 workflow run 的动作，因此本轮没有真实运行仓库 pytest；当前状态不能写 DONE。用户本地验证通过后再补本地回归 DONE 日志和 Evidence Pack regression claim。
- 本轮没有执行 real-model `planning_recovery_skills vs planning_recovery_skills_mcp` A/B，不产生 success-rate/pass@1/token/latency/MCP effectiveness 数字。
- 本轮没有实现 P2-5 Evolution、Multi-Agent、Forge MCP Server、完整 resources/prompts runtime、OAuth 平台化或 dangerous external account E2E。
- 本轮更新日志：`docs/changes/2026-09-19/P2-4-MCP-Client-Tool-Adapter.md`。

### 最后交接（2026-09-19，Chat/GitHub Issue 清理契约测试修复）

- 修复 Chat 与 GitHub Issue 相关测试替身缺少 `close()` 导致的 3 个 `AttributeError`。
- 修改 `tests/test_chat.py` 与 `tests/test_github_issue_delivery.py`，仅为 `FakeSession`、`AssertRunner` 补充空实现 `close()`；未修改生产逻辑。
- 定向测试 3 passed；Chat/GitHub Issue 相关测试共 25 passed；`git diff --check` 通过。
- 本轮更新日志：`docs/changes/2026-09-19/MCP清理契约测试替身修复.md`。
- 当前未处理用户原有未跟踪的 `evals/results/local-*` 目录；未处理 stash，未修改 `config/default.yaml`、fixture 或历史结果。

### 验证补充（2026-09-19，P2-4 MCP Client / Tool Adapter 本地收口）

- 用户已在本地完成 P2-4 修复并 push，并明确要求进入 P2-5；P2-4 状态正式由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 收口为 `DONE`。
- 当前可核验修复提交：`dev@f842902e1753bdc69b0217d5aa89033bacd2ae82`。其中修正 MCP description 截断边界，并补齐 Chat / GitHub Issue 测试替身的 `close()` 清理契约。
- 该提交日志记录：定向测试 3 passed；Chat/GitHub Issue 相关测试 25 passed；`git diff --check` 通过。
- 用户未提供最终全量 pytest 的 passed 数量、完整 stdout 或耗时，因此不得补造数字。
- 本轮没有执行 real-model `planning_recovery_skills vs planning_recovery_skills_mcp` A/B；不得宣称 MCP 提升 success rate、pass@1、token efficiency、latency 或任意外部 MCP server 的生产可靠性。
- 验证日志：`docs/changes/2026-09-19/P2-4-MCP-Client-Tool-Adapter本地回归-DONE.md`。
- P2 Agent Intelligence 现在只剩 P2-5 Trajectory-driven Skill Evolution。


### 最后交接（2026-09-19，P2-5 Trajectory-driven Skill Evolution）

- P2-5 首版实现已落地，当前状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`；实现主提交为 `af363b2acb536e8d9b169a57bce826e7ef6c0412`，provenance/path hardening 提交为 `62076b792cfd18d5bb4473000a1538a4e4206e7c`，最终交接以当前 dev 最新 HEAD 为准。
- 新增 `experience/schema.py / trajectory.py / candidate.py / store.py / evaluation.py / promotion.py`；P2-5 是 post-run offline subsystem，不修改 `ExecutionRunner → Agent → ToolExecutor` 主执行链，也不新增第二套 Agent loop。
- Trajectory 不复制 transcript：Trace v2 是 process canonical source，P2-0 TrialResult/GraderResult 是 eval judgment；`TrajectoryRef` 只保存 run/task/trace hash/ref 与可选 trial correlation。
- positive mining 只接受成功且 independent acceptance passed 的 trajectory；cancel、infrastructure failure、incomplete/gave_up、未请求/未通过 acceptance 不生成成功经验。Recovery pattern 直接消费 P2-2 typed `failure_classified / recovery_selected / plan_revised`，不从自然语言猜。
- Candidate 使用 P2-3 标准 `SKILL.md`，但隔离在 repository-bounded `.forge-agent/experience/` store，普通 Agent/SkillCatalog 默认看不到。Candidate/evaluation/decision 为 immutable artifact，state 独立维护；stable identity 排除本机 artifact path，并对重复 source evidence 去重。
- Candidate evaluation 复用 P2-0 `EvaluationHarness`，正式比较 baseline 与 candidate-enabled 两个 variant；fixture suite 独立覆盖 target、should-trigger、should-not-trigger、non-regression，不修改 P2-0 frozen 8-case suite。
- deterministic PromotionGate 区分 `PASS / REJECT / INSUFFICIENT_EVIDENCE / EVALUATION_FAILED`，检查 evidence count、outcome/non-regression、trigger/process、token/step overhead 和 stale hash/version；evaluation infrastructure failure 不算 candidate reject。
- `PromotionManager.promote()` 是显式动作：必须验证 persisted PASS decision、matching evaluation record、candidate status/hash/version。首版只支持 project Skill；promotion/rollback 校验 project boundary 与父目录 symlink，用户手工 Skill 无 Forge evolution provenance 时禁止覆盖；managed Skill 支持 parent version/hash 升级校验与 approved snapshot rollback。
- P2-5 不修改 source Trace、不创建 Trace v3；offline lifecycle 另写 bounded `evolution_events.jsonl`，只保存 candidate/eval/run/trace hash/reference 等 metadata，不重新塞完整 trajectory、prompt 或 Skill 内容。
- 新增 `tests/test_skill_evolution.py`、`evals/fixtures/skill_evolution/`，并在 `pyproject.toml` 加入 `experience*` package/coverage discovery。
- ChatGPT 容器已实际运行 `py_compile` 与 standalone core smoke，均通过；由于容器无法解析 github.com，未运行当前仓库级 pytest。不得把本轮写成 DONE，也不得宣称 Skill Evolution 提升真实 coding success、pass@1、token/latency 或长期智能。
- 本轮更新日志：`docs/changes/2026-09-19/P2-5-Trajectory-driven-Skill-Evolution.md`。


### 验证补充（2026-09-19，P2-5 Trajectory-driven Skill Evolution 本地收口）

- 用户已在本地对当前已推送实现 HEAD `ac5f30d4951cef8d2840fdbd8bbebe71352f1e48` 完成 P2-5 专项、P2-3/P2-2/P2-1/P2-0、Trace/Runner/Failure Harness 回归、Evidence Pack 与全量 pytest，并明确确认全部通过。
- 最终通过轮次未提供具体 passed 数量、完整 stdout 或耗时，因此不得补造数字。
- P2-5 状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 正式收口为 `DONE`。
- deterministic evidence 覆盖 success+acceptance eligibility、cancel/infra/unverified exclusion、typed failure→recovery mining、deterministic/input-order-independent grouping、source evidence 去重、candidate identity/version/hash、CandidateStore 隔离/边界/损坏检测、P2-0 EvaluationHarness baseline vs candidate overlay、target/should-trigger/should-not-trigger/non-regression、PromotionGate 四状态、regression/overhead/stale hash、persisted PASS decision、显式 project promotion、manual Skill collision、managed Skill upgrade/rollback、evolution audit 与 package discovery。
- 本轮没有执行 real-model candidate A/B；不得宣称 Skill Evolution 提升 success rate、pass@1、trigger accuracy、token/step efficiency、latency 或长期 self-improvement。
- P2 Agent Intelligence 的 P2-0 ～ P2-5 至此全部完成；后续若做统一 architecture ablation，应继续按 Evidence Pack 区分 deterministic regression 与 real-model small sample。
- 验证日志：`docs/changes/2026-09-19/P2-5-Trajectory-driven-Skill-Evolution本地回归-DONE.md`。


### 最后交接（2026-09-20，Planning v2 / Recovery v2 基础设施收口）

- 本轮起点为 Stage 1 DONE 的 `dev@509ad31099639e84c9e8a06114b8df58100840a6`；实现链依次为 `d6da718ea9957c8153cd33a0f541adbb137d21b3`、`49d01e502d2673e59e07dd13b260996a53ddaf8e`、`f850097f625817fe9868c97527e1f0b2444ab5ca`，最终交接仍以当前 dev 最新 HEAD 为准。
- Planning v2：模型只负责 goal/description/targets/verification 等语义规划；`PlanningRuntime` 确定性生成稳定 step identity，维护 version/status/lineage 与合法 transition。无 plan 时只暴露 `plan_create`；已有 plan 时只暴露 `plan_step_update / plan_revise`，并把合法 step ids 写入动态 enum。
- terminal step 重复确认同一状态改为 accepted idempotent no-op，Trace 写 `idempotent=true,state_changed=false`；terminal rollback 仍拒绝。Eval 只统计真实 `state_changed=true` 的 completed transition，避免重复调用膨胀 process metric。
- Recovery v2：`recovery_max_attempts` 作为 per-category budget；另保留 global hard ceiling，避免无界恢复。Trace 同时记录 category occurrence / category max / global attempt / global max；不同 failure category 不再互相吞掉小预算。
- Semantic progress：repository content change、首次 Skill load、真实 plan create/revise、新 test evidence 都可推进；重复 Skill load、Planning no-op、重复相同 test evidence、普通 file_read 不会无限刷新。该逻辑继续复用现有 Agent loop，没有引入第二套 planner/recovery loop。
- Provider strict schema capability 独立于 `ModelCapabilities`。新增 `llm/tool_schema.py`；OpenAI Chat / Responses 可通过 `llm.strict_tool_schema=on` 显式启用 strict conversion，`auto` 对未协商能力的 OpenAI-compatible/native endpoint 均保守关闭；Anthropic 继续使用原生 `input_schema`。
- strict conversion 会递归满足 object `required/additionalProperties` 约束，并把 Forge 原本 optional 的字段转为 nullable；provider 返回 optional `null` 后，Core 在 Runtime validation 前按原始 schema 去掉这些 placeholder，既保留 strict provider contract，也不改变 Tool 的旧默认参数语义。
- Core 新增单一 `_active_tool_schemas()`，system prompt 看到的 Planning/Skill schema 与真正传给 backend 的 tools 保持一致；之前只把 control schema 写进 prompt、但 provider tools 仅来自 ToolRegistry 的分叉已收口。
- `config/default.yaml` 本轮按明确要求增加 `strict_tool_schema: auto`，当前兼容代理不会被默认强行 strict。没有修改 API Key 内容、B1/B2 fixture 或历史 `evals/results`。
- 新增/扩展 regression：`tests/test_structured_planning.py`、`tests/test_structured_recovery.py`、`tests/test_agent_skills.py`、`tests/test_tool_schema_strictness.py`，并继续覆盖 Completion Guard / CLI / OpenAI Responses / Model-aware Token Budget / API / Chat / GitHub Issue 接线。
- 当前 ChatGPT 执行容器无法解析 github.com，仓库也没有可由当前 connector 直接启动的新 workflow；因此本轮尚未真实运行当前候选 HEAD 的 pytest，不得写 DONE 或虚构 passed 数量。状态为 `IMPLEMENTED / LOCAL VALIDATION PENDING`。
- 本轮实现日志：`docs/changes/2026-09-20/P2-Stage1-Planning-Recovery-v2收口.md`。本地应先跑日志中的专项回归，再跑全量 pytest；全部通过后再执行 `Napabana/pr-test:forge-p2-skill-demo` 真实模型 E2E，并另补验证 DONE 日志。


### 修复补充（2026-09-20，Semantic Progress control-action guard）

- 本地专项回归暴露 `tests/test_agent_skills.py::test_skill_load_is_semantic_progress_but_duplicate_load_is_not` 失败：预期 1 次 NO_PROGRESS，实际为 0。
- 根因不是 Skill semantic-progress 去重失效；`agent/core.py` 对重复 Skill/Planning control 已正确增加 `steps_without_semantic_progress`，但 control branch 随后直接 `continue`，没有执行统一 NO_PROGRESS threshold。原测试后续第三次相同 `file_read` 又会先触发 LoopDetector，使 NO_PROGRESS 更难被观察到。
- 修复：新增 `Agent._apply_no_progress_guard()`，普通 Tool、Skill control、Planning control 共用同一 threshold/recovery/reflection 逻辑；重复 `skill_load` 或 Planning no-op 现在不能靠 control-path `continue` 无限逃避 progress guard。
- 测试改为在第二次重复 `skill_load` 当步就要求 NO_PROGRESS，移除会与 LoopDetector 竞争的第三次相同 read，并断言 NO_PROGRESS 与 duplicate Skill load 处于同一 step。
- 此补丁仍需用户本地重跑专项与全量 pytest；ChatGPT 环境未执行 pytest，不得宣称通过。


### 修复补充（2026-09-20，全量回归：filesystem trace pollution / MCP crash fixture）

- 用户本地全量回归结果：`4 failed, 973 passed, 14 skipped, 34 warnings`。
- `tests/test_day2.py::TestReflectionNoEdit::test_reflection_triggered_after_no_edit_steps` 与 `tests/test_runner.py` 两个 acceptance/completion 失败同源：非 Git fallback `repository_content_fingerprint()` 把 repo 内 `logs/` EventLog 当成 repository content。Trace 每次 append 都伪造 semantic progress，并让 require_changes completion guard 误判仓库已变化。
- 修复 `context/repository_state.py`：非 Git filesystem fingerprint 忽略顶层 `logs/`，与既有 LoopDetector 的 runtime-state ignore contract 对齐；Git 仓库路径仍由 `git ls-files` 决定，tracked `logs/` 不会被该 fallback 规则静默忽略。
- 新增 `tests/test_repository_state.py`：直接锁定 runtime log append 不改变 non-Git content fingerprint，同时真实文件修改仍必须改变 fingerprint。
- `tests/test_mcp_integration.py::test_stdio_server_process_crash_maps_to_remote_capability_and_closes` 在全量 suite 中于 `manager.start()` 握手阶段 2s 超时，未进入其真正要测的“已发现 tool 后 process crash → REMOTE_CAPABILITY”路径。生产 MCP manager 未修改；仅把 crash fixture timeout 对齐正常 stdio fixture 的 5s startup budget。
- 当前补丁仍需用户本地复跑失败 4 项、相关专项和全量 pytest；ChatGPT 环境未运行 pytest，不得宣称 DONE。


### 验证补充（2026-09-20，Planning v2 / Recovery v2 本地回归 DONE）

- 用户已在本地基于 `dev@ab0f1b26ff6c79c5c12f6418185607ab939a658c` 完成前述失败点复验、相关专项与全量 pytest，并明确确认“全过”。
- 最终通过轮次未提供具体 passed / skipped 数量、完整 stdout 或耗时，因此不得补造数字。
- Planning v2 / Recovery v2 / Semantic Progress / provider-aware strict tool schema 本轮状态由 `IMPLEMENTED / LOCAL VALIDATION PENDING` 正式收口为 `DONE`。
- 本地 deterministic regression 已覆盖 runtime-owned plan step identity、dynamic planning schema、terminal idempotency、revision lineage、per-category recovery budget + global hard ceiling、Skill/Plan/Test semantic progress、control-action NO_PROGRESS guard、non-Git runtime log pollution、Completion Guard、Runner acceptance 边界、MCP crash fixture 以及 strict schema adapter/config 接线。
- 前一轮全量暴露的 4 个失败已经经过修复后重新验证全部通过；不得继续把 `4 failed, 973 passed...` 当作当前状态。
- 当前尚未执行本轮 Planning/Recovery v2 的新 real-model E2E，因此不得宣称 step/token/latency、success-rate 或 pass@1 已改善。
- 下一步：使用全新 clone/reset 的 `Napabana/pr-test:forge-p2-skill-demo` 执行真实模型 E2E，重点核验 malformed planning control、runtime step identity、idempotent terminal update、semantic progress 与 recovery category accounting；完成后另补 E2E 验证日志。
- 验证日志：`docs/changes/2026-09-20/P2-Stage1-Planning-Recovery-v2本地回归-DONE.md`。


### 验证补充（2026-09-20，Planning v2 / Recovery v2 真实模型 E2E DONE）

- 用户已在全新/独立的 `Napabana/pr-test:forge-p2-skill-demo` 工作目录完成真实模型 E2E；模型为 `deepseek-v4.1-flash`，OpenAI-compatible 路径，最终 `SUCCESS`。
- 本次结果：15 steps、89,485 tokens、89.0s；focused pytest 4 passed、full pytest 19 passed、repository release verifier 输出 `release contract: OK`，且未创建 Git commit。
- 真实链路：`skill_load verify-release-contract → read implementation/tests/verifier → plan_create → focused test failure → failure_classified(test_failure) → recovery_selected(inspect) → file_write → focused pass → full pass → plan step completion → release verifier pass → remaining plan step completion → FINISH`。
- Trace 确认 Runtime 生成 4 个稳定 step ids：`fix-normalize-label-to-normalize-all-whitespace-to-kebab-case`、`run-focused-pytest-file`、`run-full-pytest-suite`、`run-release-contract-verifier`；四次 completion 均为 `state_changed=true,idempotent=false`。
- Trace 确认 Recovery accounting：`test_failure` 的 `category_occurrence=1`、`category_max_attempts=4`、`global_attempt=1`、`global_max_attempts=12`。
- 用户提供的过滤 Trace 未出现 `plan_rejected`；本次可确认没有可见 malformed planning control。真实 run 也没有发生 plan revision、recovery exhaustion 或 completion rejection。
- 本次没有触发 terminal duplicate update，因此 `state_changed=false,idempotent=true` 的真实模型路径没有被本次 run 覆盖；该契约仍由 deterministic regression 覆盖，不能写成 real-model evidence。
- 与 Stage 1 旧 run（18 steps / 122,597 tokens / 98.6s）相比，本次单样本为 15 / 89,485 / 89.0s。只能作为同 fixture 的 small-sample observation，不能据此宣称总体 success-rate、pass@1 或稳定 token/latency 改善。
- Planning v2 / Recovery v2 至此同时具备 deterministic regression DONE 与 real-model E2E DONE 证据。
- 验证日志：`docs/changes/2026-09-20/P2-Stage1-Planning-Recovery-v2真实模型E2E-DONE.md`。


### 修复补充（2026-09-21，P2-4 MCP real-model r1：Provider failure + Eval workspace isolation）

- 用户提交真实模型 MCP Eval artifact：`evals/results/p2-4-mcp-real-e2e-r1/`。该 trial 确实执行（不是 not_executed），配置为 `planning_recovery_skills_mcp`、`deepseek-v4.1-flash`、MCP server `eval_docs`。
- Trace 已证明 MCP stdio server 正常启动并协商 capability：protocol `2026-07-28`，tools/resources/prompts 均 supported，共发现 5 个 MCP tools。
- trial 最终不是 MCP failure，而是第 4 次 model turn 经 connection/timeout retry 后收到 Provider/Cloudflare 522；RunResult 正确收口为 `termination_reason=provider_error`，acceptance skipped。该 r1 不得计作 MCP capability failure。
- r1 同时暴露 Eval isolation bug：`find_files` / `search_text` 未绑定 target workspace，默认从 Forge Agent 进程 cwd 搜索。模型因此看到了 `evals/fixtures/coding_agent/mcp_suite.json` 中的 reference expectation `strict`，trajectory 被 benchmark leakage 污染。虽然 `mcp-guidance-used` grader 要求真实调用 MCP、能够阻止该 run 误判 PASS，但 r1 仍不能作为干净 MCP real-model evidence。
- 修复：`SearchTextTool / FindFilesTool / FindSymbolTool` 新增可选 workspace；production registry 与 file tools 一样注入 target repo/worktree workspace。无 path 时默认 search target workspace；显式绝对/相对 path 逃逸 workspace 时返回 `invalid_arguments`。
- 新增 registry regression：process cwd 中放置外部 fixture，三个 search tools 默认只能看到 workspace 内容，并拒绝 absolute/relative escape。
- 配置加载同时收紧：显式传入 `--config` 但文件不存在时不再 silent fallback 到默认空配置，而是抛出 `FileNotFoundError`；路径含反斜杠时提示 Linux/WSL 使用 `/`。
- 本轮补丁需要用户本地跑定向测试和全量回归后，再用新 output dir 执行 MCP real-model r2。不得复用/覆盖 r1，也不得把 r1 的 0 success 当作 MCP outcome。


### 修复补充（2026-09-21，P2-4 MCP pr-test real E2E：guidance topic normalization）

- 用户在 `Napabana/pr-test:forge-p2-mcp-demo` 上完成真实 MCP direct-run；MCP tool 被真实调用 6 次，最终 Step 26 的 topic `policy` 返回 `POLICY_MODE='strict'`，随后成功修改 `src/app/config.py` 并验证 runtime 输出 `'strict'`。
- Run 最终为 `INCOMPLETE`（30 steps / 343,717 tokens / 339.8s），原因是 max_steps，而非 MCP transport/call failure。
- 根因：固定 MCP eval fixture 的 `lookup_project_guidance(topic)` 原先只对精确 key `policy` 返回 policy guidance；`policy_mode`、`policy mode configuration`、`organization policy`、`required policy mode value` 都退回 generic navigation。真实模型因此连续重复 MCP lookup，并触发 NO_PROGRESS / replan / 额外 repository exploration。
- 这属于 eval fixture 的 exact-key trap，不应把“猜中一个隐藏枚举 key”混入 MCP capability 验收。修复后 topic 先统一 underscore/hyphen/whitespace，再按自然语义关键词路由；包含 `policy` 的查询稳定返回 policy guidance，config/test 查询同理。
- `tests/test_mcp_integration.py` 的真实 stdio fixture regression 新增 `required policy mode value` 与 `policy_mode` 两种自然查询，均必须得到 strict policy guidance。
- 本轮不同时修改 shell Git conservative classification、Planning gate 或 max_steps；这些是在重复无效 guidance 后出现的次生行为，避免把 MCP fixture 修复扩成无关控制面改造。
- 需要用户本地先跑 MCP/CLI 定向回归，再 reset/reclone `pr-test:forge-p2-mcp-demo` 执行新的 real-model E2E。


### 验证补充（2026-09-21，P2-4 MCP pr-test real-model E2E DONE）

- 用户在独立目标仓库 `Napabana/pr-test:forge-p2-mcp-demo` 上完成第二轮真实模型 MCP direct-run；Forge Agent 只提供 Agent/MCP runtime，业务读写目标为 pr-test。
- 修复 natural topic routing 后，Step 1 首次调用 `mcp__eval_docs__lookup_project_guidance`（topic=`policy mode configuration`）即返回 `Set POLICY_MODE = 'strict' in src/app/config.py`，不再依赖猜中 magic key `policy`。
- Agent 随后读取 pr-test 的 `src/app/config.py` / `runtime.py` / test，创建 plan，将 canonical config 从 `legacy` 修改为 `strict`，并在最终修改后执行全量 `tests/`：16 passed。
- 最终行为验证输出 `policy_mode -> strict`，未创建 Git commit，Run 以 `SUCCESS / completion_satisfied` 收口。终端结果：29 steps、295,016 tokens、779.2s。
- 本轮真实证明：MCP server discovery → model selects namespaced MCP tool → ToolExecutor lifecycle → MCPToolAdapter → MCP server structured result → model consumes external guidance → pr-test repository edit → post-edit tests → FINISH/Completion Guard。
- `Acceptance: not_requested` 仅表示 Runner 没有额外 independent AcceptanceContract；Agent Core 的 completion guard 仍依据 require_changes/require_tests/final repository content state 执行，最终 SUCCESS 说明这些内建完成性条件已满足。
- 本轮不能作为 MCP 性能改善证据：模型在 CRLF/trailing-newline preservation 上产生多次 file_write/shell 检查，并出现一次 LLM timeout；29 steps / 295k tokens / 779.2s 只能作为成功轨迹，不用于 token/latency efficiency claim。
- pr-test clone 在运行前已有 Windows/WSL line-ending 导致的工作树噪声（如 `.gitignore`）；它没有提供 policy 正确答案，也不否定 MCP capability 结果，但后续正式 benchmark 应使用 `core.autocrlf=false` + clean reset 的冻结 fixture。
- 本轮没有提供可核验的 Docker/sandbox trace，因此不得把 host filesystem isolation 写成 real-model verified；search/file workspace isolation 已由 deterministic regression 覆盖，shell sandbox 仍属于已有独立 runtime contract。
- P2-4 MCP 至此具备 deterministic regression + real-model pr-test E2E 双层证据，可正式收口为 DONE。后续进入 P2-5 Trajectory-driven Skill Evolution 验收/实操。
- 验证日志：`docs/changes/2026-09-21/P2-4-MCP-pr-test-Real-Model-E2E-DONE.md`。


### 收口补充（2026-09-21，MCP E2E 后噪声治理：IMPLEMENTED / LOCAL VALIDATION PENDING）

- pr-test 测试分支 `forge-p2-mcp-demo` 新增 `.gitattributes`：`* text=auto eol=lf`，用于冻结 benchmark checkout 的 LF 语义，提交 `0de0c7cc8d947999900e65c2c25b30c9ebfa3af1`。
- Forge 新增 `FileEditTool`：唯一 exact replacement，基于 raw bytes 替换，保留未修改区域的 CRLF/LF、EOF newline、BOM；workspace / Permission 路径边界与 file_write 一致。提交 `164513ee523a236b5ec63d897a7d5fb2585cb0a7`。
- Shell fail-safe 收紧：`python -c/python3 -c`、`find`、`awk`、`sed -n` 不再按简单只读前缀处理；PermissionManager 与 Planning 统一复用 `_is_repository_readonly`，未知/表达能力强的命令保守 CONFIRM。提交 `f658b1e35bd974461bf16c60f3d83f521c6bd3b4`。
- 只读 pipeline 支持：`cat | tail`、`git diff | cat`、`wc | tail` 只有在所有 stage 都可证明只读时标记 READ_ONLY；重定向、command substitution、background、`||`、未知/可变更 stage 仍 fail-safe。提交 `d1edd08401285ee981a5cf0b623b680cbb284411`。
- System Prompt 明确：有专用 repo tool 时优先于 shell；existing file localized change 优先 file_edit；没有 tests/repo policy/diff 证据时不要检查 CRLF/EOF newline；post-edit verification 通过后避免重复检查并 FINISH。提交 `7ecbdc3b8cc7e3c6e8288c07460babbcc79998bd`。
- Semantic Progress 新增 MCP evidence：首次成功且内容唯一的 MCP observation 可推进 semantic progress；完全相同的重复 MCP output 不重复刷新进度。提交 `c1d3ed24b388e90b034be51f61baf66928ce4326`。
- 本轮只完成实现与 deterministic tests 补充；当前执行环境无法联网拉取仓库，因此没有在这里真实运行 pytest。用户本地验证前不得写“tests passed”。
- 推荐定向测试：`tests/test_day3.py tests/test_cli_isolate.py tests/test_harness.py tests/test_structured_planning.py tests/test_structured_recovery.py tests/test_repo_map_prompt_layout.py tests/test_mcp_integration.py`，随后再跑全量 `python -m pytest -q`。
- 该轮目标是收敛 MCP real-model run 中的非 Provider 噪声，不产生任何 token/latency improvement claim；需要重新跑 clean pr-test E2E 后才能比较轨迹。


### 验证补充（2026-09-21，MCP E2E 后噪声治理：VERIFIED）

- 用户本地已确认本轮修改后的全量 pytest 全部通过；此前逐项回归暴露并修复了 Shell fake-fixture、`find` action 分类与正则转义问题，最终 `dev@36d1150ea4958de6bd64e44c5c4713e0e9aa8ab6` 进入 real-model 复测。
- 使用与上一轮相同 task、相同 `deepseek-v4.1-flash`、相同 `config/eval-p2-mcp-pr-test.yaml` 和 clean `pr-test:forge-p2-mcp-demo` 进行 r3。
- r3 结果：`SUCCESS`，11 steps，73,197 tokens，75.1s；修改后 full `tests/` 为 16 passed。
- 轨迹：Step 1-2 repo exploration → Step 3 单次 MCP guidance 成功返回 strict/canonical path → Step 4 plan_create → Step 5-6 plan step updates → Step 7 `file_edit` 精确修改 config → Step 8 full tests 16 passed → Step 9 status/diff 验证 → Step 10 plan step update → Step 11 FINISH。
- 与上一轮同 fixture 成功 run（29 steps / 295,016 tokens / 779.2s）相比，本次单次运行 steps -18（-62.07%）、tokens -221,819（-75.19%）、wall time -704.1s（-90.36%）。
- 这些差值只能表述为“同一 fixture 的单次 r3 相比 r2 显著更低”，不能推广为稳定性能提升或统计结论；上一轮还包含一次 provider timeout，而本轮没有。
- 可直接归因到轨迹的治理效果：`file_edit` 替代重复 `file_write`；没有 xxd/od/CRLF/trailing-newline 修复链；没有 permission_denied/no_progress/replan；MCP guidance 只调用一次；post-edit full test 后立即收口。
- 仍有可优化项：Step 1-2/9 仍使用 shell 做 repo inspection/status/diff，而系统 Prompt 已要求优先专用 tools；本轮不继续扩大范围，后续可在正式 benchmark 中观察 tool-choice adherence。
- 噪声治理状态从 `IMPLEMENTED / LOCAL VALIDATION PENDING` 更新为 `VERIFIED / REAL-MODEL R3 PASS`。
