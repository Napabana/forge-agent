# Forge Agent

Forge Agent 是一个本地运行的自主编程智能体。它能够读取目标仓库、调用
LLM 分析任务、修改文件、执行命令和测试，并通过 JSONL 事件日志记录完整过程。

当前支持 Anthropic、OpenAI、DeepSeek、Groq 和 Ollama，提供一次性任务、
连续对话、Docker 沙箱、Git worktree 隔离和 GitHub Issue 自动处理入口。

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

## 项目结构

```text
forge-agent/
├── agent/
│   ├── core.py                 # 同步 ReAct 主循环和 AgentConfig
│   ├── event_log.py            # JSONL 事件写入、回放与统计
│   ├── orchestrate.py          # worktree、TaskEngine、权限和沙箱组合根
│   ├── prompt.py               # System prompt 与上下文组装
│   └── task.py                 # Task、Action、Observation、RunResult
├── config/
│   ├── default.yaml            # 默认模型、预算、工具和上下文配置
│   └── schema.py               # YAML、.env 加载及类型化配置
├── context/
│   ├── history.py              # 对话历史窗口
│   ├── repo_map.py             # tree-sitter 仓库符号摘要
│   └── token_budget.py         # token 估算、分配和裁剪
├── entry/
│   ├── chat.py                 # 多轮 ChatSession
│   ├── cli.py                  # agent run/chat/log 命令
│   └── github_issue.py         # GitHub Issue 到 Pull Request 流程
├── harness/
│   ├── executor.py             # 统一工具执行管线
│   ├── hooks.py                # 工具执行前后钩子
│   └── permission.py           # ALLOW、CONFIRM、DENY 权限决策
├── ipc/
│   └── bus.py                  # 异步 AgentBus 发布订阅总线
├── llm/
│   ├── anthropic_backend.py    # Anthropic 原生 tool_use 后端
│   ├── base.py                 # LLMBackend 接口与 MockBackend
│   ├── openai_compat.py        # OpenAI-compatible 与文本解析后端
│   └── router.py               # Provider 路由和环境变量解析
├── runtime/
│   └── worktree.py             # Git worktree 事务生命周期
├── task/
│   └── engine.py               # SQLite 任务状态机与并发认领
├── tools/
│   ├── base.py                 # BaseTool 与 ToolRegistry
│   ├── file_tool.py            # 受 workspace 约束的文件工具
│   ├── git_tool.py             # Git 状态、差异、暂存和提交
│   ├── runtime.py              # LocalRuntime 与 DockerRuntime
│   ├── search_tool.py          # 文件、文本和符号搜索
│   ├── shell_tool.py           # Shell 执行与危险命令检查
│   └── test_tool.py            # pytest 执行和结果解析
├── tests/                      # 单元测试与集成测试
├── scripts/
│   ├── m1_demo.py              # TaskEngine/worktree 演示
│   ├── m4_demo.py              # 编排层集成演示
│   └── start.sh                # 当前开发环境启动脚本
├── s20_comprehensive/          # 参考实现、说明和架构图
├── pyproject.toml              # 包元数据、依赖和 agent CLI 入口
├── smoke_test.py               # 端到端环境与模型联通检查
├── README.md                   # 项目说明和当前使用指南
└── USAGE.md                    # 早期完整教程（以 README 为当前准则）
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
