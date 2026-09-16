# Forge Agent 本地协作与交接

本文件是本机 Codex/Agent 的持续交接说明。每次开始任务先读，结束任务前更新“最后交接”。
这是面试项目：优先保证代码可验证、设计可解释、指标不夸大。

## 不可违反

- 保留用户已有修改；先看 `git status` 和 diff，禁止擅自 reset、clean、checkout 覆盖。
- 默认在 `dev` 开发；切分支、pull、rebase、push 前先确认工作区和跟踪分支。
- 修改代码前先明确需求、影响文件、理由和验证计划；若用户已像本轮一样明确授权某一范围可直接实施，则无需逐文件重复确认，但不得越过授权范围。
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

若 Linux venv 尚未创建，用 `python3 -m venv ~/.venvs/forge-agent`，再安装 `-e ".[dev]"`。
Windows venv 不要直接在 WSL 中复用。

`KRILL_API_KEY` 应保存在 Windows 用户环境变量，或 WSL 的 `~/.config/forge-agent/env`（权限 `600`），绝不写真实值到 YAML。

## 项目认知

- `agent/core.py`：同步 ReAct 主循环、完成性守卫、Reflection、循环检测。
- `agent/runner.py`：CLI/Chat/API/GitHub Issue 的统一 execution composition root，负责 acceptance 与统一 post-run Trace。
- `agent/event_log.py`：append-only JSONL 审计与 Trace v2 写盘边界。
- `agent/trace_v2.py`：Trace v2 schema 常量、entrypoint/session 传播和统一 redaction。
- `llm/`：Anthropic、OpenAI-compatible Chat Completions、OpenAI Responses。
- `context/`：ConversationHistory、Repo Map、Token Budget、Compaction。
- `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py`：四个产品入口。
- `task/engine.py`：SQLite WAL 任务状态与条件认领。
- `runtime/worktree.py`、`agent/orchestrate.py`：隔离 worktree、成果检测和保留策略。
- `tools/`、`harness/`：工具执行、Hook、Permission 与本地/Docker runtime。

面试表述边界：

- 同步的是 `Agent.run` 内核，asyncio 主要用于编排。
- Worktree 隔离 checkout/index/branch；Docker 隔离进程、网络、资源和根文件系统。
- 保留 worktree 不等于已经 commit、merge、push 或创建 PR。
- EventLog 是审计记录，不是确定性重放。
- Trace v2 是 Forge 自有最小 schema，不是完整 OpenTelemetry implementation。
- 本地 token breakdown 是诊断 estimate；provider usage 才是 provider 返回的真实 usage。

## 当前状态（2026-09-16）

当前唯一状态清单：`TODO-P0-P1.md`。当前实施顺序：`Forge-Agent-P0-P1-实施计划.md`。

已完成：

- P0-1：`prepare_next_turn` / shared-history 边界。
- P0-3：Trace v2 最小闭环。
- P1-1：统一 Runner、独立 acceptance、确定性交付。
- P1-2：Context Compaction C1-C5、B1、B2；C6 延期。
- P1-3：Session 加固。
- P1-5：Repo Map 核心能力与正式消融。

仍为 `PARTIAL`：

- P0-2：Tool Hook / Permission / Cancel 生产语义。
- P1-4：固定 Harness failure injection 任务集。
- P1-6：面试证据包产品化。

明确延期：完整 Resource Manager、hidden-verifier feedback、C6 `context_recall(event_ref)`、MCP、多 Agent、multi-tool call、tree-structured session、自动 merge/无人监督发布。

### P0-3 Trace v2 当前事实

- 新写入 EventLog 统一 `trace_schema_version=2`，保留 `schema_version=2` 兼容别名。
- Run 使用 `run_id/run_span_id`；Model/Tool/Context 使用 child span 与 operation id。
- Runner 统一追加 acceptance 与 `run_terminated`；GitHub Issue 在 commit/push/PR 结果后追加 delivery。
- CLI/Chat/API/GitHub Issue 通过统一 Runner 解析 entrypoint；isolate 路径用轻量 ContextVar 传播到深层 EventLog 创建点。
- EventLog 最终 JSONL 写盘边界递归 redaction，覆盖 Authorization/API Key/GitHub token/password/secret/token 与 Bearer、`sk-`、`ghp_`、`github_pat_` 等常见字符串模式。
- usage token 字段显式保护，不会因为字段名含 `token` 被删除。
- Model span 记录 `token_breakdown`（system/tool schema/repo map/history/context/pending/injected/estimated input）与独立 `provider_usage`。
- provider error、infrastructure error、cancel、loop detected、resource exhausted、completion rejection、acceptance、delivery 可在同一 run correlation 下审计。
- 旧 JSONL 可直接 replay；历史行不迁移不重写，新 append 才使用 v2 metadata。
- B1/B2 历史结果和 fixture 未改写。

完整说明：`docs/changes/2026-09-16/Trace-v2收口改动内容.md`。

### 当前分支与远程事实

- 远程工作分支：`dev`。
- P0-3 实现基线：`18fb0cc39b09d428a069b79c3f925875bcdd7ffe`。
- 本轮通过 GitHub connector 直接提交到 `dev`；用户本地尚未 pull/执行 pytest。
- `config/default.yaml` 未修改。
- 历史 `evals/results` 未重写。
- 当前 GitHub 仓库没有本轮可用的 CI status；connector 本身不能运行仓库 pytest。

## 已知问题与下一步

下一批只做 P0-2，不提前做 P1-4/P1-6：

1. 以 `harness/executor.py` 为中心画清 `pre-hook → permission → tool → post-hook` 的真实顺序。
2. 建立 CLI / Chat / API / GitHub Issue 的 Hook/Permission 行为矩阵。
3. 明确 cancel 在 pre-hook 前、permission 后、tool 前后、post-hook 前后的行为与最终 RunStatus。
4. 将 permission deny、hook block/failure、tool failure、cancel 稳定映射到 Trace v2 tool event 与 termination reason。
5. 只修复可复现不一致，不重构 Agent 核心生命周期，不修改 `config/default.yaml`。

P0-2 完成后再新开对话做 P1-4 failure Harness；之后做 P1-6 面试证据产品化。

不要为了简历堆功能。每个新增主张必须能指向实现、测试或可复现实验；测试覆盖率不等于 Agent 真实任务成功率。

## P0-3 本地验证

用户 pull 后先运行：

```bash
pytest tests/test_trace_v2.py tests/test_runner.py tests/test_compaction.py \
  tests/test_agent_completion_guards.py tests/test_chat.py tests/test_api.py \
  tests/test_github_issue_delivery.py -q
```

然后验证旧 B1/B2 reader：

```bash
pytest tests/test_context_policy_benchmark.py \
  tests/test_context_policy_agent_ablation.py -q
```

最后：

```bash
pytest -q
```

若失败：保留原始失败输出，不修改 fixture 或历史 result；按失败节点重新打开 P0-3。

## 每次结束：更新最后交接

必须在本文件末尾覆盖更新以下内容：

- 本轮目标和结论。
- 实际修改的文件。
- 测试命令与真实结果。
- 未提交修改、stash、分支和 remote 状态。
- 明确的下一步或阻塞原因。

### 最后交接（2026-09-16，P0-3 Trace v2）

- 本轮完成 P0-3 Trace v2 收口。实现从 `dev@18fb0cc39b09d428a069b79c3f925875bcdd7ffe` 开始，代码与测试已直接提交到远程 `dev`。
- 生产代码涉及：`agent/trace_v2.py`、`agent/task.py`、`agent/event_log.py`、`agent/core.py`、`agent/runner.py`、`entry/github_issue.py`。
- 测试涉及：`tests/test_trace_v2.py`、`tests/test_github_issue_delivery.py`；既有 `tests/test_runner.py`、`tests/test_compaction.py`、`tests/test_agent_completion_guards.py`、`tests/test_chat.py`、`tests/test_api.py` 与 B1/B2 测试作为本地回归集。
- 状态文档已更新：`TODO-P0-P1.md` 将 P0-3 标为 `DONE`，实施计划下一批切到 P0-2；`docs/README.md` 已索引本轮日志。
- 未修改 `config/default.yaml`，未修改 B1/B2 fixture，未重写 `evals/results`，未开始 P0-2/P1-4/P1-6。
- 当前会话无法真正运行 pytest：GitHub connector 只有仓库读写/提交能力；容器环境也无法解析 `github.com` 完成 clone。因此测试状态必须记为“测试代码已补齐，远程未执行”，不能写成通过。
- 用户下一步：本地 `git pull` 后执行“P0-3 本地验证”三组命令；若通过即可冻结 P0-3。若失败，把失败输出带到下一条消息，先修 P0-3 再进入 P0-2。
- 完整记录：`docs/changes/2026-09-16/Trace-v2收口改动内容.md`。
