# Forge Agent

Forge Agent 是一个面向软件工程任务、在本地运行的自主编程智能体引擎。它以
同步 ReAct 循环为控制内核，通过任务状态机、Git worktree 事务、权限 Harness
和可选 Docker 沙箱完成代码分析、文件修改、命令执行、测试与结果审计。

项目支持 Anthropic、OpenAI、DeepSeek、Groq 和 Ollama，并同时适配 Anthropic
Messages、OpenAI Chat Completions 与 OpenAI Responses 协议。它提供 CLI 一次性
任务、连续对话、HTTP API 与 GitHub Issue 自动处理入口。每次运行都可写入 JSONL
事件日志；隔离模式还会记录任务状态、worktree 生命周期和权限决策。

## 核心能力

- **多入口**：CLI、Chat、FastAPI 和 GitHub Issue 流程复用同一套执行内核。
- **事务化隔离**：SQLite WAL TaskEngine 管理状态，WorktreeSession 管理临时工作区。
- **集中式权限控制**：工具调用统一经过 Hooks、PermissionManager 和 ToolExecutor。
- **双运行时**：支持宿主机执行，也支持带资源限制和路径白名单的 Docker 沙箱。
- **可审计与可回放**：EventLog 持久化执行过程，AgentBus 实时转发事件。
- **多协议模型后端**：Anthropic 原生工具调用、OpenAI-compatible Chat Completions
  和 Responses API 共享统一的 `LLMBackend`/`Action` 边界。
- **中转站兼容**：容忍空 choices/delta/message/usage 和非标准流式结束帧，并可用
  `--no-stream` 快速区分上游故障与流式协议差异。

## 架构设计

Forge Agent 将同步决策循环与异步 I/O 边界分开。`Agent.run()` 保持同步，负责
步骤、token 预算和工具调用顺序；`orchestrate_run()` 是隔离执行的异步组合根，
负责 TaskEngine、worktree、AgentBus 和沙箱生命周期，并通过线程边界运行 Agent。

普通 `run`/`chat` 可以直接在目标仓库运行；`run --isolate` 和 API 任务通过事务
编排层运行：

```text
CLI / Chat / HTTP API / GitHub Issue
                 │
                 ├── 普通模式 ──────────────────────────────────┐
                 │                                             │
                 └── 隔离模式 → orchestrate_run                 │
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

下面的结构来自当前仓库的 `tree -a` 输出。省略 `.git/` 和 `tests/` 内部文件，
其余目录与文件均按实际用途标注：

```text
forge-agent/
├── .agents/                    # 本地 Agent 扩展预留目录；当前为空，运行时未引用
├── .codex/
│   └── config.toml             # Codex 项目级沙箱配置
├── .forge/                     # API 运行状态，不属于源码
│   └── api_tasks.db            # HTTP 请求生命周期与 Agent 日志映射数据库
├── .worktrees/                 # isolate 模式创建临时 Git worktree 的默认父目录
├── agent/                      # Agent 控制内核、领域模型和组合根
│   ├── __init__.py             # Python 包标识
│   ├── core.py                 # 同步 ReAct 主循环、完成保护和步骤控制
│   ├── event_log.py            # JSONL 事件追加、查询、回放与统计
│   ├── loop_detector.py        # 重复动作、无进展和工具调用循环检测
│   ├── orchestrate.py          # TaskEngine/worktree/bus/权限/沙箱异步组合根
│   ├── prompt.py               # System prompt 与任务上下文组装
│   └── task.py                 # Task、Action、Observation、Event、RunResult
├── coding_agent.egg-info/      # editable install 生成的包元数据；可重新生成
│   ├── PKG-INFO                # 项目名称、版本、依赖等元数据快照
│   ├── SOURCES.txt             # 构建系统收录的源文件清单
│   ├── dependency_links.txt    # setuptools 兼容依赖链接元数据
│   ├── entry_points.txt        # agent CLI 入口映射
│   ├── requires.txt            # 安装依赖与可选依赖清单
│   └── top_level.txt           # 安装后暴露的顶层 Python 包
├── config/                     # 应用配置定义与加载
│   ├── default.yaml            # Provider、模型、预算、工具和上下文默认值
│   └── schema.py               # YAML/.env 解析、dataclass 配置和 CLI 覆盖
├── context/                    # 注入 ReAct 循环的上下文管理
│   ├── __init__.py             # Python 包标识
│   ├── history.py              # 对话历史窗口、追加与裁剪
│   ├── repo_map.py             # tree-sitter 仓库符号摘要
│   └── token_budget.py         # token 估算、预算分配与截断
├── entry/                      # 用户和外部系统入口
│   ├── api.py                  # FastAPI 路由、worker、SSE 与 dashboard
│   ├── api_store.py            # HTTP 任务生命周期 SQLite 存储
│   ├── chat.py                 # 多轮 ChatSession
│   ├── cli.py                  # agent run/chat/log 命令及工具注册
│   └── github_issue.py         # GitHub Issue → Agent → Pull Request 流程
├── harness/                    # 工具调用的安全拦截管线
│   ├── __init__.py             # Python 包标识与公共接口出口
│   ├── executor.py             # Hooks → Permission → Tool 的统一执行器
│   ├── hooks.py                # 工具执行前后扩展点
│   └── permission.py           # ALLOW/CONFIRM/DENY 与 workspace 路径边界
├── ipc/                        # 进程内异步通信
│   ├── __init__.py             # Python 包标识与 AgentBus 导出
│   └── bus.py                  # asyncio.Queue topic 发布/订阅总线
├── llm/                        # 模型后端抽象与 Provider 适配
│   ├── __init__.py             # Python 包标识
│   ├── anthropic_backend.py    # Anthropic 原生 tool_use 后端
│   ├── base.py                 # LLMBackend 接口、响应类型和 MockBackend
│   ├── errors.py               # 可重试、过载和协议错误类型
│   ├── openai_compat.py        # OpenAI-compatible、流式响应和文本动作解析
│   ├── openai_responses.py     # OpenAI Responses 文本、工具调用与事件流适配
│   └── router.py               # Provider、base URL 与 API key 路由
├── runtime/                    # 代码工作区事务
│   ├── __init__.py             # Python 包标识与 worktree 接口导出
│   └── worktree.py             # Git worktree 创建、绑定、统计和异常清理
├── scripts/                    # 开发与里程碑演示脚本
│   ├── m1_demo.py              # TaskEngine 与 WorktreeSession 事务演示
│   ├── m4_demo.py              # 编排、权限、事件和清理闭环演示
│   └── start.sh                # 当前机器的 venv/.env/CLI 启动辅助脚本
├── task/                       # 持久化任务状态层
│   ├── __init__.py             # Python 包标识与 TaskEngine 类型导出
│   └── engine.py               # SQLite WAL DAG、状态转换和并发原子认领
├── tests/                      # 自动化测试；内部文件按要求不在此展开
├── tools/                      # Agent 可调用工具与命令运行时
│   ├── __init__.py             # Python 包标识
│   ├── base.py                 # BaseTool、ToolResult 与 ToolRegistry
│   ├── file_tool.py            # 文件读取、创建、替换和路径校验
│   ├── git_tool.py             # Git status/diff/add/commit 工具
│   ├── runtime.py              # Runtime、LocalRuntime 与 DockerRuntime
│   ├── sandbox.Dockerfile      # 默认 Python Docker 沙箱镜像
│   ├── search_tool.py          # 文件、文本与代码符号搜索
│   ├── shell_tool.py           # Shell 执行、超时、截断与危险命令识别
│   └── test_tool.py            # pytest 执行与结果解析
├── .gitignore                  # Git 忽略规则：缓存、日志、密钥和运行产物
├── README.md                   # 当前架构、安装、使用和开发说明
├── USAGE.md                    # 补充使用教程
├── linked_list.py              # Agent 生成能力示例：单链表实现
├── pyproject.toml              # 包元数据、依赖、CLI、pytest 与 coverage 配置
├── quicksort.py                # Agent 生成能力示例：多种快速排序实现
└── smoke_test.py               # 配置、模型后端和工具链联通检查
```

从职责上看，`agent/` 是同步控制核心；`task/`、`runtime/` 和 `ipc/` 提供事务与
异步基础设施；`harness/` 和 `tools/` 构成受控执行边界；`llm/` 与 `context/`
提供决策输入；`entry/` 负责把这些能力暴露给 CLI、HTTP 和 GitHub。

以下内容是本地状态或可再生成产物：

- `.forge/` 保存 API 任务状态，删除会丢失已有 API 任务记录。
- `.worktrees/` 由隔离运行创建并在结束时清理；异常残留可在确认无任务运行后处理。
- `coding_agent.egg-info/` 由 `pip install -e .` 生成，可以删除并重新安装恢复。
- `logs/`、`__pycache__/`、`.pytest_cache/` 等运行缓存不属于项目源码，应保持忽略。

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

Chat Completions（默认兼容模式）：

```bash
export OPENAI_API_KEY="sk-..."
agent chat --provider openai --model gpt-4o
```

Responses API（OpenAI 官方新接口或仅开放 `/responses` 的中转渠道）：

```bash
agent chat --provider openai --protocol responses --model gpt-5.4
```

`base_url` 应填写到 API 版本根路径，例如 `https://api.example.com/v1`；不要把
`/responses` 写进 `base_url`，SDK 会自动追加资源路径。

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
  protocol: auto               # auto | chat_completions | responses
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

协议选择规则：

| `protocol` | 后端 | 典型场景 |
| --- | --- | --- |
| `auto` | Anthropic 走 Messages，其余走 Chat Completions | 默认，兼容旧配置 |
| `chat_completions` | `OpenAICompatBackend` | OpenAI、DeepSeek、Groq、Ollama及多数中转站 |
| `responses` | `OpenAIResponsesBackend` | OpenAI Responses 或仅开放 `/responses` 的渠道 |

配置加载器可用`FORGE_ENV_FILE=/path/to/.env` 指定；shell 中已经存在的环境变量不会
被 `.env` 覆盖。

## 使用指南

### 交互对话

`chat` 会保留本次会话的上下文，并在每轮任务前确认有风险的命令：

```bash
agent chat
agent chat --repo /path/to/project
agent chat --provider deepseek --model deepseek-chat
agent chat --provider openai --protocol responses --model gpt-5.4
agent chat --no-stream                    # 排查中转站流式响应兼容问题
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
agent run --repo . --protocol responses --no-stream --task "读取配置并总结"
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
  [--protocol auto|chat_completions|responses]
  [--model MODEL]
  [--max-steps N]
  [--stream | --no-stream]
  [--sandbox]
  [--verbose]

agent run
  (--task TEXT | --task-file FILE)
  [--repo PATH]
  [--provider PROVIDER]
  [--protocol auto|chat_completions|responses]
  [--model MODEL]
  [--max-steps N]
  [--stream | --no-stream]
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

**中转站提示 GPT 只支持 Responses API**

在 YAML 中设置 `protocol: responses`，或在命令行加入 `--protocol responses`。
如果只是把 `--stream` 改成 `--no-stream`，请求仍会发送到原来的协议端点。

**中转站返回 `500/502` 或流式响应结构不完整**

先用 `--no-stream` 复测。非流式成功通常表示流式事件格式不兼容；非流式仍返回
`500/502` 通常是模型名称、渠道权限或上游服务问题。后端会安全跳过空
`choices`、空 `delta/message`，并在缺少 `usage` 时估算 token，不再因这些响应
形态直接触发 `NoneType.content`。

**Docker 沙箱无法启动**

运行 `docker version` 检查客户端能否连接 daemon，并确认当前用户有权使用
Docker。首次使用还需要能够拉取 `python:3.11-slim`。

**`--isolate` 创建 worktree 失败**

确认目标目录是 Git 仓库、至少有一次提交，且 `.worktrees/` 中没有同名的残留
目录。
