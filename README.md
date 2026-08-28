# Forge Agent

Forge Agent 是一个面向软件工程任务、在本地运行的自主编程智能体引擎。它以
同步 ReAct 循环为控制内核，通过任务状态机、Git worktree 事务、权限 Harness
和可选 Docker 沙箱完成代码分析、文件修改、命令执行、测试与结果审计。

项目支持 Anthropic、OpenAI、DeepSeek、Groq 和 Ollama，并提供 CLI 一次性任务、
连续对话、HTTP API 与 GitHub Issue 自动处理入口。每次运行都可写入 JSONL 事件
日志；隔离模式还会记录任务状态、worktree 生命周期和权限决策。

## 核心能力

- **多入口**：CLI、Chat、FastAPI 和 GitHub Issue 流程复用同一套执行内核。
- **事务化隔离**：SQLite WAL TaskEngine 管理状态，WorktreeSession 管理临时工作区。
- **集中式权限控制**：工具调用统一经过 Hooks、PermissionManager 和 ToolExecutor。
- **双运行时**：支持宿主机执行，也支持带资源限制和路径白名单的 Docker 沙箱。
- **可审计与可回放**：EventLog 持久化执行过程，AgentBus 实时转发事件。

## 架构设计

Forge Agent 将同步决策循环与异步 I/O 边界分开。`Agent.run()` 保持同步，负责
步骤、token 预算和工具调用顺序；`orchestrate_run()` 是隔离执行的异步组合根，
负责 TaskEngine、worktree、AgentBus 和沙箱生命周期，并通过线程边界运行 Agent。

普通 `run`/`chat` 可以直接在目标仓库运行；`run --isolate` 和 API 任务通过事务
编排层运行：

```text
CLI / Chat / HTTP API / GitHub Issue
                 │
                 ├── 普通模式 ───────────────────────────────┐
                 │                                            │
                 └── 隔离模式 → orchestrate_run               │
                                  ├── TaskEngine (SQLite WAL)  │
                                  ├── WorktreeSession          │
                                  └── AgentBus                 │
                                                               ▼
                         Agent.run (同步 ReAct 控制内核)
                                  ├── Context / Token Budget
                                  ├── LLMBackend / LoopDetector
                                  └── ToolExecutor
                                        ├── Hooks
                                        ├── PermissionManager
                                        └── ToolRegistry
                                              └── LocalRuntime / DockerRuntime
                                                               │
                                                               ▼
                                              EventLog (JSONL 审计与回放)
```

架构遵循四项约束：控制内核保持同步；异步资源集中在组合根；任务状态与 worktree
生命周期绑定；路径和命令权限在执行器边界统一收口。EventLog 是审计事实来源，
隔离模式可将新增事件转发到 AgentBus，供 CLI、SSE 或其它订阅者消费。

## 项目结构

```text
forge-agent/
├── agent/                      # 控制内核、领域模型、事件日志和异步组合根
│   ├── core.py                 # 同步 ReAct 主循环、步骤控制和 AgentConfig
│   ├── orchestrate.py          # TaskEngine/worktree/bus/权限/沙箱组合根
│   ├── task.py                 # Task、Action、Observation、Event、RunResult
│   ├── prompt.py               # System prompt 与任务上下文组装
│   ├── loop_detector.py        # 重复动作、无进展和循环调用检测
│   └── event_log.py            # JSONL 事件追加、回放与统计
├── task/engine.py              # SQLite WAL DAG 任务机与原子状态转换
├── runtime/worktree.py         # Git worktree 事务生命周期和异常清理
├── ipc/bus.py                  # asyncio.Queue 发布/订阅事件总线
├── harness/                    # Hooks、权限决策和统一工具执行管线
│   ├── executor.py
│   ├── permission.py
│   └── hooks.py
├── tools/                      # Agent 可调用的工具与命令运行时
│   ├── base.py                 # BaseTool、ToolResult、ToolRegistry
│   ├── file_tool.py            # 文件查看和修改
│   ├── shell_tool.py           # Shell 执行和危险命令识别
│   ├── search_tool.py          # 文件、文本和符号搜索
│   ├── test_tool.py            # pytest 执行与结果解析
│   ├── git_tool.py             # Git 状态、差异、暂存和提交
│   ├── runtime.py              # LocalRuntime 与 DockerRuntime
│   └── sandbox.Dockerfile      # 默认 Python 沙箱镜像
├── llm/                        # LLM 抽象、Provider 实现、路由和错误类型
│   ├── base.py
│   ├── anthropic_backend.py
│   ├── openai_compat.py
│   ├── router.py
│   └── errors.py
├── context/                    # 历史窗口、仓库地图和 token 预算
├── entry/                      # CLI、Chat、HTTP API 和 GitHub Issue 入口
│   ├── cli.py
│   ├── chat.py
│   ├── api.py                  # FastAPI、worker、SSE 和 dashboard
│   ├── api_store.py            # HTTP 任务生命周期 SQLite 存储
│   └── github_issue.py
├── config/                     # 默认 YAML、.env 加载和类型化配置
├── tests/                      # 单元、并发、集成和 Docker 沙箱测试
├── scripts/                    # M1/M4 演示及本地启动脚本
├── .codex/config.toml          # Codex 项目级沙箱配置
├── pyproject.toml              # 包、依赖、pytest 和 agent CLI 配置
├── smoke_test.py               # 配置、模型和工具链联通检查
├── linked_list.py              # Agent 生成能力示例
├── quicksort.py                # Agent 生成能力示例
├── README.md                   # 当前架构、安装和使用说明
└── USAGE.md                    # 补充使用教程
```

运行时还可能生成以下非源码目录：

- `logs/`：JSONL 事件日志和隔离模式 TaskEngine 数据库。
- `.forge/`：HTTP API 任务元数据，例如 `api_tasks.db`。
- `__pycache__/`、`.pytest_cache/`、`*.egg-info/`：可重新生成的 Python 缓存。

## 环境要求

- Python 3.11 或更高版本
- Git
- 对应模型服务的 API key；使用 Ollama 时不需要 key
- Docker（仅 `--sandbox` 需要）
- 目标仓库至少有一次 Git 提交（仅 `--isolate` 需要）

## 安装

```bash
git clone https://github.com/Napabana/forge-agent.git
cd forge-agent

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

安装完成后应能看到 CLI 帮助：

```bash
agent --help
```

也可以不使用已安装的命令，直接执行：

```bash
python -m entry.cli --help
```

## 模型配置

默认配置位于 [`config/default.yaml`](config/default.yaml)。推荐把密钥放在环境
变量中，不要写入仓库。

### Anthropic

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export MODEL_ID="claude-sonnet-4-5"
agent chat --provider anthropic
```

### OpenAI

```bash
export OPENAI_API_KEY="sk-..."
agent chat --provider openai --model gpt-4o
```

### DeepSeek

```bash
export DEEPSEEK_API_KEY="sk-..."
agent chat --provider deepseek --model deepseek-chat
```

### Groq

```bash
export GROQ_API_KEY="gsk_..."
agent chat --provider groq --model llama-3.3-70b-versatile
```

### Ollama

先启动本地 Ollama 服务，再运行：

```bash
agent chat --provider ollama --model qwen2.5-coder
```

配置加载优先级为：内置默认值 < YAML 配置 < CLI 参数。可以通过全局
`--config` 参数使用自己的配置文件：

```bash
agent --config /path/to/agent.yaml chat --repo /path/to/project
```

配置文件格式：

```yaml
llm:
  provider: anthropic
  model: ${MODEL_ID}
  api_key: ${ANTHROPIC_API_KEY}
  base_url: ${ANTHROPIC_BASE_URL}
  max_tokens: 8192

agent:
  max_steps: 40
  budget_tokens: 80000
  log_dir: ./logs

tools:
  shell:
    timeout: 30
    max_output_tokens: 8000
  file:
    max_view_lines: 100

context:
  repo_map_budget: 8000
  history_window: 20
```

配置加载器可用`FORGE_ENV_FILE=/path/to/.env` 指定；shell 中已经存在的环境变量不会
被 `.env` 覆盖。

## 使用指南

### 交互对话

`chat` 会保留本次会话的上下文，并在每轮任务前确认有风险的命令：

```bash
agent chat
agent chat --repo /path/to/project
agent chat --provider deepseek --model deepseek-chat
agent chat --repo /path/to/project --max-steps 60 --verbose
```

会话内命令：

| 命令 | 作用 |
| --- | --- |
| `/stats` | 查看会话轮数、步骤数和 token 统计 |
| `/clear` | 清除对话历史，保留初始仓库上下文 |
| `/help` | 显示会话命令 |
| `/exit`、`/quit`、`/q` | 退出 |

### 一次性任务

```bash
agent run --repo /path/to/project --task "修复失败的单元测试"
agent run --repo /path/to/project --task-file task.txt
agent run --repo . --task "重构解析器" --max-steps 60
agent run --repo . --task "更新依赖" --confirm
```

`--confirm` 会在危险 shell 命令执行前请求确认。未启用时仍会经过内置权限和
命令安全检查，但不会对所有需要确认的操作进行交互询问。

### Docker 沙箱

在宿主机已安装并启动 Docker 后：

```bash
docker version
agent run --repo /path/to/project --task "运行并修复测试" --sandbox
agent chat --repo /path/to/project --sandbox
```

沙箱使用 `python:3.11-slim`，默认限制为 1 GiB 内存、2 个 CPU，关闭容器
网络并挂载 `/tmp` 临时文件系统。目标仓库挂载到容器内 `/workspace`。
首次运行可能需要 Docker 拉取镜像。

注意：默认断网意味着智能体不能在沙箱中下载依赖。`python:3.11-slim` 也不一定
包含目标项目所需的系统工具和依赖，复杂项目应准备自己的预构建镜像或先在本地
模式验证。

### Git worktree 隔离

```bash
agent run --repo /path/to/git-repo \
  --task "验证修复方案" \
  --isolate

agent run --repo /path/to/git-repo \
  --task "在容器和临时工作树中验证修复" \
  --isolate --sandbox
```

`--isolate` 会创建临时 worktree，并通过 SQLite TaskEngine 记录任务状态。当前
实现会在运行结束或异常时强制清理临时 worktree 及其分支，适合验证隔离、权限
边界和审计流程。需要保留代码修改时，请使用普通 `run` 或 `chat` 模式。

### 查看事件日志

运行日志默认写入 `./logs`：

```bash
agent log list
agent log list --dir /path/to/logs
agent log show logs/<task-id>_<timestamp>.jsonl
```

日志包含任务、动作、工具观察、反思和最终状态。`--isolate` 模式还会记录任务
认领、worktree 生命周期和权限决策。

### GitHub Issue 自动处理

```bash
export GITHUB_TOKEN="github-token"

python -m entry.github_issue \
  --repo owner/repository \
  --issue 42 \
  --local-path /tmp/repository
```

该入口会读取 Issue、克隆或复用本地仓库、创建工作分支、运行 agent，并在成功
后推送分支和创建 PR。常用选项：

```bash
# 只在本地运行，不推送或创建 PR
python -m entry.github_issue \
  -r owner/repository -i 42 -l /tmp/repository --no-pr

# 指定目标分支和配置
python -m entry.github_issue \
  -r owner/repository -i 42 -l /tmp/repository \
  --base-branch develop --config /path/to/agent.yaml
```

`GITHUB_TOKEN` 需要具备读取 Issue、推送分支和创建 Pull Request 所需的仓库
权限。

### HTTP API 服务

Forge Agent 也可以作为轻量级 HTTP 后端运行，方便接入 Web 前端、CI 或其它
服务。API 层不会替代 CLI；它复用现有 agent、TaskEngine、Git worktree 隔离和
JSONL 事件日志。

安装 API 依赖：

```bash
pip install -e ".[api,dev]"
```

启动服务：

```bash
uvicorn entry.api:app --reload
```

浏览器访问 `http://127.0.0.1:8000/` 会返回 API 索引；访问
`http://127.0.0.1:8000/docs` 可以打开 FastAPI 自动生成的交互式文档。
也可以访问 `http://127.0.0.1:8000/dashboard` 使用内置的轻量任务面板。

提交任务：

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "repo_path": "/path/to/repo",
    "prompt": "修复失败的 pytest",
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "max_steps": 40,
    "sandbox": false
  }'
```

查询状态和事件：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/tasks
curl http://127.0.0.1:8000/tasks/<task_id>
curl http://127.0.0.1:8000/tasks/<task_id>/events
curl -N http://127.0.0.1:8000/tasks/<task_id>/events/stream
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/cancel
```

第一版 API 默认在后台线程池执行任务，并使用 `orchestrate_run` 走隔离
worktree、权限检查和事件审计链路。API 模式下不会交互确认危险命令；
需要确认的命令会被权限管线拒绝。

可用环境变量：

```bash
# 后台 worker 数，默认 2
export FORGE_API_WORKERS=2

# queued/running/cancel_requested 任务总数上限，默认 50
export FORGE_API_QUEUE_LIMIT=50

# 限制 API 只能运行这些目录下的仓库；多个路径用系统 path separator 分隔
export FORGE_API_ALLOWED_ROOTS="/home/jfm/projects:/tmp/demo-repos"
```

当前 API 已支持：

- SSE 事件流：`GET /tasks/{task_id}/events/stream`
- 协作式取消：`POST /tasks/{task_id}/cancel`
- worker 数、队列长度和仓库 allowlist 配置
- 简单 Web dashboard：`GET /dashboard`

剩余限制：

- 取消是协作式的，会在 agent 下一轮 LLM/工具调用前停止；已经进入单个长
  shell 命令时不会强杀该命令。
- dashboard 是调试面板，不包含鉴权、多用户隔离或持久化前端状态。

## 命令参考

```text
agent [--config PATH] COMMAND

agent chat
  [--repo PATH]
  [--provider PROVIDER]
  [--model MODEL]
  [--max-steps N]
  [--sandbox]
  [--verbose]

agent run
  (--task TEXT | --task-file FILE)
  [--repo PATH]
  [--provider PROVIDER]
  [--model MODEL]
  [--max-steps N]
  [--confirm]
  [--sandbox]
  [--isolate]
  [--verbose]

agent log list [--dir DIR]
agent log show LOG_FILE
```

## 安全边界

- 文件工具将相对路径限制在当前目标仓库或临时 worktree 内，并拒绝路径逃逸。
- Shell 工具有拒绝、确认和允许三类权限决策。
- `chat` 默认提供危险命令确认回调；`run` 通过 `--confirm` 启用交互确认。
- `--sandbox` 隔离命令执行环境，但文件工具仍由宿主进程执行，并受 workspace
  路径边界约束。
- 使用普通本地模式时，允许的命令直接以当前用户权限在宿主机执行。请先提交或
  备份目标仓库中的重要修改。

## 开发与测试

```bash
source .venv/bin/activate
pip install -e ".[dev]"

# 非 Docker 测试
pytest -k "not DockerRuntimeIntegration"

# 包含 Docker 集成测试，需要 Docker daemon 和镜像
pytest

# 单个测试文件
pytest tests/test_orchestrate.py
```

可选安装更多 tree-sitter 语言和精确 token 统计支持：

```bash
pip install -e ".[full]"
```

## 常见问题

**`python` 命令不存在**

使用 `python3.11` 创建虚拟环境并激活；激活后再使用 `python` 和 `agent`。

**提示 API key 缺失**

确认 provider 对应的环境变量已经导出，或确认 `FORGE_ENV_FILE` 指向的文件可读。

**Docker 沙箱无法启动**

运行 `docker version` 检查客户端能否连接 daemon，并确认当前用户有权使用
Docker。首次使用还需要能够拉取 `python:3.11-slim`。

**`--isolate` 创建 worktree 失败**

确认目标目录是 Git 仓库、至少有一次提交，且 `.worktrees/` 中没有同名的残留
目录。
