# Forge Agent 使用手册

这份手册按“从零配置 → 逐条跑通真实流程 → 检查产物与边界”的顺序编写。目标不是只告诉你命令怎么写，而是让你能把当前 `dev` 的核心产品路径完整验收一遍：

1. `agent run` direct；
2. `agent chat` 多轮 Session；
3. `agent run --isolate` Git Worktree；
4. `agent run --sandbox` 与 `--isolate --sandbox` Docker；
5. GitHub Issue → Agent → Acceptance → PR；
6. EventLog / Trace v2 / Session / Worktree 产物检查。

项目整体架构与实现文件说明见 [`README.md`](README.md)。

## 1. 运行前准备

### 1.1 获取 `dev`

```bash
git clone https://github.com/Napabana/forge-agent.git
cd forge-agent
git checkout dev
git pull --ff-only
```

如果你已经有本地仓库，先确认：

```bash
git status --short --branch
git log -3 --oneline --decorate
```

不要在有未提交重要修改的目标仓库上直接做第一次验收。建议另外准备一个小型、已提交、测试可运行的 Git 仓库作为被测项目。

### 1.2 Python 环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

验证 CLI：

```bash
agent --help
agent run --help
agent chat --help
```

需要完整语言解析与 tiktoken：

```bash
pip install -e ".[full,dev]"
```

需要 API：

```bash
pip install -e ".[api,dev]"
```

### 1.3 API Key

真实 Key 不要写进仓库。可以直接使用 shell 环境变量，也可以写在仓库外：

```bash
mkdir -p ~/.config/forge-agent
chmod 700 ~/.config/forge-agent
cat > ~/.config/forge-agent/env <<'EOF'
YOUR_API_KEY=replace-me
EOF
chmod 600 ~/.config/forge-agent/env
```

如果想使用其它 env 文件：

```bash
export FORGE_ENV_FILE=/path/to/private.env
```

`config/schema.py` 会先读取 shell 环境变量；仓库外 env 不覆盖已经存在的环境变量。

## 2. 配置模型与 Context Budget

Forge 当前把“模型能力”和“本次 Forge 请求策略”分开处理。

推荐自己的配置文件，例如 `~/forge-agent-local.yaml`：

```yaml
llm:
  provider: openai
  protocol: chat_completions
  model: your-model
  api_key: ${YOUR_API_KEY}
  base_url: https://api.example.com/v1

  # 确认真实能力时才填写：
  context_window: 128000
  model_max_output_tokens: 8192

  # 本次请求实际允许的最大输出：
  max_output_tokens: 8192

agent:
  max_steps: 40
  context_budget_cap: 80000
  context_safety_margin_tokens: 1024
  log_dir: ./logs

context:
  repo_map_budget: 8000
  history_window: 20
  semantic_packet_max_tokens: 16000

tools:
  shell:
    timeout: 30
    max_output_tokens: 8000
  file:
    max_view_lines: 100
```

调用时全局指定：

```bash
agent --config ~/forge-agent-local.yaml run --repo /path/to/repo --task "..."
agent --config ~/forge-agent-local.yaml chat --repo /path/to/repo
```

### 2.1 不知道代理站真实窗口怎么办

未知 OpenAI-compatible 代理不要仅凭模型名填写 `context_window`。可以只配置：

```yaml
llm:
  max_output_tokens: 8192

agent:
  context_budget_cap: 80000
  context_safety_margin_tokens: 1024
```

此时 80k 是 Forge 自己的输入预算 cap fallback，不代表模型真实 Context Window。

### 2.2 旧字段仍能用

当前为了兼容历史配置：

```yaml
llm:
  max_tokens: 8192
agent:
  budget_tokens: 80000
```

分别兼容为 request `max_output_tokens` 和 Forge `context_budget_cap` fallback。新配置建议使用显式字段。

### 2.3 Protocol

- `anthropic` provider 的 `auto` 会走 Anthropic Messages；
- OpenAI / DeepSeek / Groq / Ollama 与多数兼容代理的 `auto` 走 Chat Completions；
- 明确要求 `/responses` 的渠道使用 `protocol: responses`。

中转站流式兼容异常时，先在同一任务上加 `--no-stream` 做对照，不要把“关闭流式”误当成切换协议。

## 3. 建议的验收目标仓库

为了能明确观察修改、测试与 Worktree，准备一个小型 Git 仓库：

```bash
mkdir -p /tmp/forge-agent-manual-test
cd /tmp/forge-agent-manual-test
git init

cat > calculator.py <<'EOF'
def add(a, b):
    return a - b
EOF

cat > test_calculator.py <<'EOF'
from calculator import add


def test_add():
    assert add(2, 3) == 5
EOF

git add .
git commit -m "test: initial broken calculator"
python -m pytest -q
```

最后一条测试应该失败。后续示例假设：

```bash
export TARGET_REPO=/tmp/forge-agent-manual-test
```

每轮验收前建议把仓库恢复到你预期的基线，并确认：

```bash
cd "$TARGET_REPO"
git status --short
git log -1 --oneline
```

## 4. 流程一：`agent run` direct

### 4.1 运行

在 Forge Agent 仓库中：

```bash
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "calculator.py 的 add 实现错误。修复它，并运行 test_calculator.py 验证测试通过。"
```

需要危险命令人工确认时：

```bash
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "修复 calculator.py 并运行测试" \
  --confirm
```

### 4.2 这条命令实际走什么路径

```text
entry/cli.py
  ↓ load_config / create_backend / ToolRegistry
ExecutionRunner
  ↓ direct mode
Agent.run
  ├─ Model-aware TokenBudget
  ├─ Persistent Query-aware Repo Map
  ├─ LLMBackend
  └─ ToolExecutor
       ├─ validation
       ├─ hooks
       ├─ PermissionManager
       └─ ToolRegistry
  ↓
EventLog / Trace v2
  ↓
RunResult
```

Direct run 不创建 Worktree。Agent 的文件写入就是目标仓库的真实 working tree 修改。

### 4.3 验收

```bash
cd "$TARGET_REPO"
git status --short
git diff
python -m pytest -q test_calculator.py
```

你应该确认：

- 原仓库直接出现修改；
- 相关测试通过；
- Forge 输出 `SUCCESS` 时，仓库里确实存在符合任务描述的修改；
- `logs/` 下新增一个 JSONL Trace。

查看日志：

```bash
cd /path/to/forge-agent
agent log list
```

取最新文件：

```bash
agent log show logs/<latest>.jsonl
```

## 5. `run` 的完成性、失败与权限行为

### 5.1 完成性守卫

如果任务描述推断出“需要修改”和/或“需要测试”，模型直接 FINISH 不一定被接受。Core 会检查真实写入、仓库变化、测试行为与 fatal runtime state。

因此测试时不要只看模型最后一句话，要同时检查：

```bash
git diff
python -m pytest -q
agent log show ...
```

### 5.2 `--confirm` 的真实语义

产品路径始终经过 PermissionManager。对需要 CONFIRM 的命令：

- `run --confirm`：调用终端确认；
- `run` 不加 `--confirm`：没有 confirm callback，CONFIRM 会按拒绝处理；
- 硬 DENY 规则不会因为加了 `--confirm` 就变成允许。

因此旧版“没加 `--confirm` 就直接执行危险命令”的理解是不正确的。

### 5.3 Ctrl+C / cancel

当前是 cooperative cancellation。已经开始的同步 Provider / Tool 调用不保证被立即强杀，而是在返回后的安全边界停止。

## 6. 流程二：`agent chat`

Chat 的重点不是“把 run 放进 REPL”，而是：

- 多轮共享 `ConversationHistory`；
- Session state 持久化；
- 每轮单独 EventLog；
- repository revision 变化时刷新 Repo Map；
- 高 Context pressure 时启用 `TraceableCompaction`。

### 6.1 启动

```bash
agent --config ~/forge-agent-local.yaml chat --repo "$TARGET_REPO"
```

启动后会显示 Session ID 与状态文件路径。

### 6.2 建议完整跑三轮

第一轮：

```text
先检查这个仓库，告诉我当前测试为什么失败，不要修改文件。
```

第二轮：

```text
现在修复这个问题，并运行相关测试。
```

第三轮：

```text
再检查 git diff，总结这次修改是否只影响了预期文件。
```

这个流程能检查：

- Round 2 是否能理解 Round 1 的分析；
- Round 3 是否知道前面做过的修改；
- 仓库修改后下一轮 Repo Map 是否会重新同步；
- 每轮是否生成独立 Trace。

### 6.3 Chat 内置命令

```text
/stats
/session
/new
/resume SESSION_ID
/rename NAME
/clear
/help
/exit
```

含义：

- `/stats`：累计 rounds / steps / usage；
- `/session`：查看 Session ID、title、state path；
- `/new`：建立全新上下文与 Session；
- `/resume ID`：切换到同一仓库已保存 Session；
- `/rename NAME`：给当前 Session 命名；
- `/clear`：清掉有效 LLM history，同时重置 compaction lineage；
- `/exit`：退出。

### 6.4 从新进程恢复

退出后执行：

```bash
agent --config ~/forge-agent-local.yaml chat \
  --repo "$TARGET_REPO" \
  --continue
```

这会恢复该仓库最近的 Session。

也可以指定：

```bash
agent --config ~/forge-agent-local.yaml chat \
  --repo "$TARGET_REPO" \
  --resume <SESSION_ID>
```

### 6.5 检查 Session 文件

默认路径：

```text
logs/chat/<repo_key>/<session_id>/state.json
logs/chat/<repo_key>/<session_id>/rounds/*.jsonl
```

`state.json` 是恢复状态真相源；`rounds/*.jsonl` 是审计日志。不要把 EventLog 当成 Session replay state。

### 6.6 `--no-session`

如果只想临时对话：

```bash
agent --config ~/forge-agent-local.yaml chat \
  --repo "$TARGET_REPO" \
  --no-session
```

此时 Session 显示为 ephemeral，不能 `--continue` / `--resume`。

### 6.7 Context Compaction 怎么验证

正常的小仓库/短会话不一定触发 compaction。它只在完整下一请求的 pressure 达到阈值后工作。

触发时终端可能看到：

```text
[压缩上下文]
```

对应 Trace 会记录 `context_compaction_started`、checkpoint、before/after tokens、pressure 等信息。Semantic side-call 失败时会记录失败并走结构化安全回退，而不是直接丢失 canonical history。

## 7. 流程三：Git Worktree isolate

Worktree 是最值得单独验收的一条路径，因为它和普通 run 的文件落点完全不同。

### 7.1 先恢复目标仓库 clean

```bash
cd "$TARGET_REPO"
git restore .
git clean -fd
git status --short
```

确认目标仓库至少有一次 commit：

```bash
git rev-parse HEAD
```

### 7.2 `keep-if-changed`

```bash
cd /path/to/forge-agent
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "修复 calculator.py 的 add，并运行 test_calculator.py" \
  --isolate \
  --result-policy keep-if-changed
```

成功产生改动时，最终输出应包含类似：

```text
Branch  : wt/task-...
Worktree: /.../.worktrees/task-...
Changes : ...
```

### 7.3 检查“主工作树没有被改”

```bash
cd "$TARGET_REPO"
git status --short
```

理想情况下主工作树仍 clean。

然后进入 Forge 输出的 Worktree：

```bash
cd <WORKTREE_PATH>
git status --short
git diff
python -m pytest -q test_calculator.py
```

这说明修改保留在独立 checkout/index/branch 中。

### 7.4 `discard`

再运行一轮：

```bash
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "修复 calculator.py 的 add，并运行测试" \
  --isolate \
  --result-policy discard
```

不论 Agent 是否产生修改，最终 Worktree 都应该被清理。

检查：

```bash
cd "$TARGET_REPO"
git worktree list
git branch --list 'wt/*'
```

### 7.5 isolate 的内部链路

```text
ExecutionRunner(isolate=True)
   ↓
orchestrate_run
   ↓
TaskEngine.create_task
   ↓
TaskEngine.claim_task
   ↓
WorktreeSession.create
   ↓
PermissionManager(workspace=<worktree>)
   ↓
Agent.run(task.repo_path=<worktree>)
   ↓
inspect_changes
   ↓
KEEP_IF_CHANGED / DISCARD
   ↓
WorktreeArtifact → RunResult
```

TaskEngine 默认 DB 位于 `log_dir/tasks.db`；API 使用自己的 Engine 路径。

## 8. 流程四：Docker sandbox

Docker 和 Worktree 的职责必须分开理解。

### 8.1 Docker 前置检查

```bash
docker version
docker info
```

Forge 默认 sandbox 镜像为：

```text
forge-agent-sandbox:py311
```

如果本地没有，Runtime 会使用 `tools/sandbox.Dockerfile` 构建。镜像内包含 Python 3.11、Git、pytest 等基础工具。

### 8.2 direct sandbox

```bash
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "修复 calculator.py 并运行测试" \
  --sandbox
```

这时：

- Shell / pytest / Git 命令在 Docker 中执行；
- 网络默认 `none`；
- 资源默认 1 GiB / 2 CPU；
- 目标 repo 挂载到 `/workspace`；
- direct sandbox 下 repo 是 bind mount，因此容器内写入会反映到宿主目标仓库；
- 文件工具本身仍由 Forge 进程执行，并受 workspace 边界限制。

所以 `--sandbox` 不是 Git 修改隔离功能。

### 8.3 isolate + sandbox

推荐把两者组合起来验收：

```bash
cd "$TARGET_REPO"
git restore .
git clean -fd

cd /path/to/forge-agent
agent --config ~/forge-agent-local.yaml run \
  --repo "$TARGET_REPO" \
  --task "修复 calculator.py 并运行 test_calculator.py" \
  --isolate \
  --sandbox \
  --result-policy keep-if-changed
```

这里顺序是：

```text
宿主 repo
  ↓
创建 Git Worktree
  ↓
Worktree 以 rw 挂载到 Docker /workspace
  ↓
Docker root filesystem readonly
  ↓
network none / 1 GiB / 2 CPU
  ↓
preflight
  ↓
Agent
```

主工作树不是 Agent 的可写工作区；Agent 的 Git/命令副作用落到临时 Worktree。

### 8.4 preflight

isolate+sandbox 在调用 LLM 之前检查：

```text
git --version
python3 -m pytest --version
git rev-parse --is-inside-work-tree
test -w .
```

如果失败，应看到 infrastructure failure，而不是继续花模型 token。

### 8.5 默认断网的影响

在 Docker 中让 Agent 执行：

```text
pip install <远端包>
curl <外部地址>
```

通常会因为 `--network none` 失败。这个行为是预期的。复杂项目应预构建适合自己的 sandbox image，而不是在任务运行期间依赖联网安装。

## 9. 流程五：GitHub Issue 本地修复

正式自动 PR 前，先跑 `--no-pr`，这样可以先验证 Issue → Agent 的任务转换和本地修改。

### 9.1 GitHub Token

```bash
export GITHUB_TOKEN=...
```

Token 至少需要读取 Issue；自动 PR 还需要 push branch / create PR 对应权限。

Forge 的 git clone/push 通过临时认证 header 传 Token，不会主动把 Token 写进 remote URL。

### 9.2 `--no-pr`

准备一个测试仓库和 Issue，例如 `owner/test-repo#1`：

```bash
python -m entry.github_issue \
  --repo owner/test-repo \
  --issue 1 \
  --local-path /tmp/forge-issue-test \
  --config ~/forge-agent-local.yaml \
  --no-pr
```

真实流程：

```text
fetch Issue title/body
  ↓
clone 或复用 /tmp/forge-issue-test
  ↓
创建 agent/fix-issue-1-<timestamp> 分支
  ↓
ExecutionRunner(entrypoint=github_issue)
  ↓
Agent 修改本地分支
  ↓
结束，不 push / 不 PR
```

检查：

```bash
cd /tmp/forge-issue-test
git branch --show-current
git status --short
git diff
```

注意：`--no-pr` 仍会创建 issue 工作分支，只是不执行自动 delivery。

## 10. 流程六：GitHub Issue 自动 PR

这是当前最完整的交付链路。

### 10.1 前置条件

自动 PR 模式要求：

1. `GITHUB_TOKEN` 可用；
2. 本地 repo 启动时必须 clean；
3. 必须提供 `--verify-command`；
4. 验收命令在目标 repo 根目录直接执行时返回 0 才算通过。

例如：

```bash
cd /tmp/forge-issue-test
git status --short
python -m pytest -q
```

如果 repo 已经被前一次 `--no-pr` 改过，最好换一个全新的 local path，避免 clean baseline 与分支状态混淆。

### 10.2 自动 PR 命令

```bash
rm -rf /tmp/forge-issue-delivery

python -m entry.github_issue \
  --repo owner/test-repo \
  --issue 1 \
  --local-path /tmp/forge-issue-delivery \
  --config ~/forge-agent-local.yaml \
  --base-branch main \
  --verify-command "python -m pytest -q"
```

### 10.3 这条链路为什么与普通 Agent commit 不一样

自动 PR 模式构造 ToolRegistry 后会主动移除：

```text
git_add
git_commit
```

所以 Agent 只能产生候选 working-tree 修改，不能在独立 verifier 运行前擅自改变 Git baseline。

随后：

```text
Agent RunResult == SUCCESS
  ↓
Independent verifier
  ↓ passed
acceptance_status = passed
  ↓
deliver_pull_request
  ├─ git add --all
  ├─ git commit -m "fix: resolve issue #N"
  ├─ git push --set-upstream origin <branch>
  └─ create Pull Request
```

### 10.4 失败状态

自动交付可能停在：

```text
blocked_agent
blocked_acceptance
commit_failed
no_changes
push_failed
pr_failed
delivered
```

如果 push 或 PR 创建失败，代码会尽量保留已经形成的本地 commit / pushed branch，避免把 Agent 成果静默删除。

### 10.5 验收 PR

在 GitHub 上确认：

- PR 源分支是 `agent/fix-issue-...`；
- PR 只包含预期改动；
- PR 描述包含 Issue task 与 Agent summary；
- Trace 中存在 delivery event；
- 没有 auto-merge，仍由人审查和合并。

## 11. EventLog / Trace v2 检查

### 11.1 列出日志

```bash
agent log list
```

### 11.2 单个 Run

```bash
agent log show logs/<file>.jsonl
```

### 11.3 原始 JSONL

```bash
python - <<'PY'
import json
from pathlib import Path
p = max(Path("logs").glob("*.jsonl"), key=lambda x: x.stat().st_mtime)
print(p)
for line in p.read_text(encoding="utf-8").splitlines():
    e = json.loads(line)
    print(e.get("event_type"), e.get("trace_schema_version"), e.get("run_id"))
PY
```

你要重点检查：

- `trace_schema_version=2`；
- 同一个 run 的 `run_id` 一致；
- LLM / Tool / Context child span 有 parent correlation；
- 终止路径存在 `run_terminated`；
- GitHub 自动 PR 有 acceptance / delivery；
- secret 没有原样落盘。

Trace 可以作为审计事实读取，但不是“重新执行一遍 Tool”的 deterministic replay。

## 12. Token Usage 怎么看

Chat `/stats` 与 Trace 会区分：

- input tokens；
- cached tokens；
- cache write tokens；
- output tokens；
- reasoning tokens；
- estimated calls；
- provider usage。

本地 `TokenCounter` 是请求前估算，主要用于 Context pressure；Provider usage 是请求后 accounting。不要用本地 estimate 冒充服务商账单数字。

## 13. Repo Map 怎么观察

默认 `AgentConfig.repo_map_mode="incremental"`。

第一次针对一个仓库运行时会建立持久化结构索引；同仓库后续运行会尝试 warm reuse。任务 query 变化时，结构索引可以复用，排序重新计算；仓库文件变化后会按 repository fingerprint / known changed path 刷新。

手工验收重点不是检查“生成了多少行 Repo Map”，而是：

1. 模型能否在不先遍历整个仓库的情况下找到相关文件；
2. 修改代码后下一轮是否还能看到新的结构；
3. Chat 多轮切换问题时是否会针对新 query rerank。

正式 benchmark 数字见 `docs/evidence/README.md`，不要从一次手工运行推导性能结论。

## 14. Session 并发与恢复边界

同一个 Session 的 `state.json` 有 revision。两个进程同时写同一 Session 时，旧 revision 会触发 `ChatSessionConflict`，而不是后写覆盖先写。

如果一轮在 LLM/Tool 中途进程退出：

- 启动前已经写入 `pending_round`；
- 恢复时会记录 interrupted；
- History 中加入“上一轮可能存在部分 Tool side effects”的显式提示；
- 用户应检查 repo + round log 再继续。

因此 Session recovery 不是事务回滚系统。

## 15. API（可选）

虽然本轮重点验收 `run / chat / GitHub Issue`，API 入口也复用同一个 Runner。

启动：

```bash
uvicorn entry.api:app --reload
```

检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/
```

创建 isolate task：

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d "{\"repo_path\":\"$TARGET_REPO\",\"prompt\":\"检查测试失败并修复\",\"sandbox\":false,\"result_policy\":\"keep-if-changed\"}"
```

API 支持：

```text
GET  /tasks
POST /tasks
GET  /tasks/{id}
GET  /tasks/{id}/events
GET  /tasks/{id}/events/stream
POST /tasks/{id}/cancel
GET  /dashboard
```

API 默认 isolate，不提供交互式 confirm；需要确认的命令在无人交互路径上会被拒绝。

## 16. 常见问题

### 16.1 `context_window is unknown and context_budget_cap is not configured`

给未知代理配置 Forge cap：

```yaml
agent:
  context_budget_cap: 80000
```

不要随便编一个模型窗口数字。

### 16.2 切换 `--model` 后 capability 变成 unknown

这是刻意行为。CLI 覆盖成另一个模型后，旧模型的 `context_window` / `model_max_output_tokens` 不应继续沿用。Forge 会保留 policy cap fallback。

### 16.3 中转站 streaming 出错

先对照：

```bash
agent ... --no-stream
```

非流式也失败再检查 model name、protocol、base URL 和渠道权限。

### 16.4 Docker 启动失败

```bash
docker info
docker image inspect forge-agent-sandbox:py311
```

首次默认镜像构建还需要 Docker 能完成 `tools/sandbox.Dockerfile` 的 build。

### 16.5 sandbox 中依赖下载失败

默认 `--network none`，这是预期行为。不要依赖任务期间联网安装，使用预构建镜像或先在宿主环境验证依赖。

### 16.6 Worktree 保留下来了是不是已经提交了

不是。`keep-if-changed` 只表示 worktree/branch 被保留。检查：

```bash
cd <WORKTREE_PATH>
git status --short
git log --oneline --decorate -3
```

### 16.7 GitHub Issue 自动 PR 提示必须 `--verify-command`

这是当前设计要求。自动交付必须有独立 acceptance：

```bash
--verify-command "python -m pytest -q"
```

### 16.8 GitHub Issue 提示 repo must be clean

自动 PR 不允许把运行前已有修改一起 commit。使用新的 clone/local path，或先自己处理已有 working-tree 修改。

### 16.9 Agent 说完成但返回非 SUCCESS

检查 `RunResult`、`termination_reason` 和 Trace。可能是完成性 guard、loop detector、resource exhaustion、provider/infrastructure failure 或 acceptance 层失败。

## 17. 推荐的完整验收顺序

严格按下面顺序跑，出现问题时更容易定位是哪一层：

```text
A. 安装 + 配置
   ↓
B. direct run
   ↓
C. chat 3 轮 + exit + --continue
   ↓
D. isolate keep-if-changed
   ↓
E. isolate discard
   ↓
F. direct sandbox
   ↓
G. isolate + sandbox
   ↓
H. GitHub Issue --no-pr
   ↓
I. GitHub Issue + --verify-command + PR
   ↓
J. Trace / Session / Worktree / PR 最终核对
```

## 18. 手工验收清单

### `agent run`

- [ ] 能读取目标仓库并定位问题；
- [ ] Tool 调用经过 Permission；
- [ ] 修改直接出现在目标 working tree；
- [ ] 要求测试时能执行测试；
- [ ] FINISH 与真实 repo/test 状态一致；
- [ ] 生成 Trace v2；
- [ ] `agent log show` 可读取。

### `agent chat`

- [ ] Session ID / state path 正常；
- [ ] 多轮共享上下文；
- [ ] 每轮独立 JSONL；
- [ ] `/stats` usage 正常；
- [ ] `/session` 正常；
- [ ] `/rename` 正常；
- [ ] 退出后 `--continue` 可恢复；
- [ ] repo 被改后下一轮仍能读取最新仓库状态。

### Worktree

- [ ] `--isolate` 创建 `wt/*`；
- [ ] 原工作树保持 clean；
- [ ] `keep-if-changed` 有成果时保留；
- [ ] `discard` 清理 worktree/branch；
- [ ] 最终输出的 path/branch/count 与实际一致。

### Docker

- [ ] 容器能启动；
- [ ] 默认断网；
- [ ] 1 GiB / 2 CPU 参数存在；
- [ ] direct sandbox 修改仍映射到宿主 repo；
- [ ] isolate+sandbox 修改只落在 worktree；
- [ ] preflight 先于 LLM；
- [ ] 运行结束容器被清理。

### GitHub Issue

- [ ] Issue title/body 正确进入任务；
- [ ] 自动创建 `agent/fix-issue-*` branch；
- [ ] `--no-pr` 不 push / 不 PR；
- [ ] 自动 PR 模式拒绝 dirty baseline；
- [ ] 自动 PR 模式强制 `--verify-command`；
- [ ] Agent 不自行 `git add/commit`；
- [ ] verifier 通过后才 commit/push/PR；
- [ ] PR 内容只包含预期修改；
- [ ] Trace 有 acceptance + delivery。

## 19. 回归与证据

手工验收结束后，建议再跑：

```bash
python -m evals.verify_evidence_pack
python -m pytest -q
```

如果某个流程手工失败，不要通过修改 fixture 或历史 `evals/results` 让测试“变绿”。先保留 Trace、终端输出、目标仓库状态和 Worktree，再按调用链定位：

```text
entry
 → runner
 → core
 → context / llm
 → harness
 → tool / runtime
 → orchestrate / worktree
 → trace / acceptance / delivery
```
