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
