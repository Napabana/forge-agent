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

面试表述边界：

- 同步的是 `Agent.run` 内核，asyncio 主要用于编排。
- Worktree 隔离 checkout/index/branch；Docker 隔离进程、网络、资源和根文件系统。
- cooperative cancellation 不等于能强杀任意正在执行的同步 Tool/provider/callback；已开始的调用在下一安全边界停止。
- PostToolUse 是已发生 Tool 的观察边界；post-hook failure 不覆盖真实 ToolResult。
- 保留 worktree 不等于已经 commit、merge、push 或创建 PR。
- EventLog 是审计记录，不是确定性重放。
- Trace v2 是 Forge 自有最小 schema，不是完整 OpenTelemetry implementation。
- 本地 token breakdown 是诊断 estimate；provider usage 才是 provider 返回的真实 usage。
- Failure Harness 证明的是冻结 failure contract 可 deterministic/offline 回归，不是线上可靠性、任务成功率或真实 Provider SLA。

## 当前状态（2026-09-16）

当前唯一状态清单：`TODO-P0-P1.md`。当前实施顺序：`Forge-Agent-P0-P1-实施计划.md`。

已完成：

- P0-1：`prepare_next_turn` / shared-history 边界。
- P0-2：Tool Hook / Permission / Cancel 生产语义。
- P0-3：Trace v2 最小闭环。
- P1-1：统一 Runner、独立 acceptance、确定性交付。
- P1-2：Context Compaction C1-C5、B1、B2；C6 延期。
- P1-3：Session 加固。
- P1-4：Failure Harness / deterministic failure injection。
- P1-5：Repo Map 核心能力与正式消融。

仍为 `PARTIAL`：

- P1-6：面试证据包产品化。

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

### 当前分支与远程事实

- 远程工作分支：`dev`。
- 用户确认的 P0-2 冻结提交：`1b567f5630c5176d09d274ecd1499f20b32612ef`。
- P1-4 代码实现基线：`aeac887b61d4e463dd47e2f4d270ce0f547072f3`；最终交接以当前 `dev` 最新 HEAD 为准。
- 本轮通过 GitHub connector 直接提交到 `dev`；用户本地尚未 pull/执行 P1-4 pytest。
- `config/default.yaml` 未修改。
- B1/B2 fixture 未修改，`evals/results` 未重写。
- 当前会话无法真正运行 pytest：容器 `git clone` 失败于 `Could not resolve host: github.com`，仓库也没有可用 `.github/workflows`。不得把“测试代码已补齐”写成“测试通过”。

## 已知问题与下一步

P1-4 已实现收口，下一批只做 P1-6，不提前扩 Agent 功能：

1. 先盘点已有 implementation/test/eval/PR evidence，不先写新 benchmark。
2. 把实现事实、deterministic offline regression、fixture benchmark、真实模型小样本、单个真实 PR 案例分开。
3. 为每类主张给出可复现命令、源文件和输出位置。
4. 明确每条证据能证明和不能证明的范围；不把 coverage 或单案例外推为成功率。
5. 优先做 evidence index / 面试叙事，除非发现真实证据缺口，否则不扩功能。

P1-6 不重新打开 P1-4 failure semantics；若本地 P1-4 pytest 失败，则先 reopen P1-4，修复后再进入 P1-6。

不要为了简历堆功能。每个新增主张必须能指向实现、测试或可复现实验；测试覆盖率不等于 Agent 真实任务成功率。

## P1-4 本地验证

用户 pull 后先运行：

```bash
pytest tests/test_failure_harness*.py -q

pytest tests/test_tool_lifecycle_p0_2.py \
  tests/test_harness.py \
  tests/test_runner.py \
  tests/test_trace_v2.py \
  tests/test_agent_completion_guards.py \
  tests/test_compaction.py \
  tests/test_chat.py \
  tests/test_api.py \
  tests/test_github_issue_delivery.py -q

pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q

pytest -q
```

若失败：保留原始失败输出，不修改 fixture 或历史 result；按失败节点重新打开 P1-4/P0-2/P0-3。

## 每次结束：更新最后交接

必须在本文件末尾覆盖更新以下内容：

- 本轮目标和结论。
- 实际修改的文件。
- 测试命令与真实结果。
- 未提交修改、stash、分支和 remote 状态。
- 明确的下一步或阻塞原因。

### 最后交接（2026-09-16，P1-4 Failure Harness / deterministic failure injection）

- 本轮完成 P1-4 实现收口；代码基线为 `dev@aeac887b61d4e463dd47e2f4d270ce0f547072f3`，文档提交之后以 `dev` 最新 HEAD 为实际交接点。
- 生产代码只修改 `agent/runner.py`：shared-history 首轮 prepare 复用 `Agent._prepare_next_turn`；isolate sandbox preflight 在 Runner composition root 归一为 `FAILED/infrastructure_error`。没有修改 `agent/core.py` 生命周期方向。
- 新增 `tests/test_failure_harness.py`：deterministic scripted provider + fake hook/permission/tool/cancel，覆盖 Provider/Hook/Permission/Tool/Cancel/prepare/completion/acceptance/delivery/Trace 主矩阵。
- 新增 `tests/test_failure_harness_isolate.py`：fake DockerRuntime + FakeWorktreeSession 验证 sandbox preflight，不访问 Docker、网络或 Provider。
- P1-4 没有新增 production `FailureScenario` dataclass/framework；pytest fixture/helper 与断言分离已经足够，避免过度 framework。
- P1-4 没有增加 eval CLI/report；默认回归命令为 `pytest tests/test_failure_harness*.py -q`，pytest 是 correctness source。
- Context semantic side-call failure 继续复用 `tests/test_structured_compaction.py` 既有 fallback regression，本轮没有重开 P1-2。
- P0-2/P0-3/B1/B2 语义与 fixture 保持不变；未修改 `config/default.yaml`，未修改 B1/B2 fixture，未重写 `evals/results`。
- 当前会话无法真正执行 pytest：容器无法解析 `github.com`，且仓库无可用 GitHub Actions workflow。测试状态是“代码已提交，远程未执行”，不是“通过”。
- 用户本地 pull 后必须依次执行上面的四组命令；任何 failure harness 或回归失败都应 reopen P1-4，不要修改 fixture 绕过。
- 当前 connector 只能看到远端 `dev`；无法读取用户本地工作区 `git status` / stash，因此没有对本地未提交修改或 stash 做任何处理或断言。
- 完整记录：`docs/changes/2026-09-16/P1-4-Failure-Harness收口改动内容.md`。
- 下一对话 P1-6 从“盘点现有可证明的 implementation/test/eval/PR evidence，并建立不过度宣称的 evidence taxonomy”开始；不要先写新功能。
