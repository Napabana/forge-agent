# CLAUDE.md

面向 forge-agent 开发者与 AI 助手的项目导航。先读这份，再动代码。

## 项目定位

融合 forge-agent 的模块化 ReAct 架构与 `s20_comprehensive/code.py` 的多智能体 Harness 机制，
目标是面向复杂代码修复 / 自动化软件工程（SWE）的生产级智能体引擎。当前处于「单 agent ReAct 已跑通」
阶段，正在进入 M1（核心事务机制）改造。改造蓝图见 `改进目标.md`、`任务规划.md`。

## 快速命令

```bash
# 环境（复用 learn-claude-code 的 venv，内含 forge-agent 依赖）
source /root/learn-claude-code/venv/bin/activate

# 运行（未 pip install -e . 时用模块入口；装后可用 `agent` 命令）
python -m entry.cli chat                          # 交互对话（推荐）
python -m entry.cli run --task "修复 failing 测试" # 一次性任务
python -m entry.cli run --task "..." --sandbox    # Docker 沙箱

# 测试
pytest                       # 全量（同步测试）
pytest tests/test_day4.py    # 单文件

# 联通验证
python smoke_test.py
```

## 密钥与配置约定（重要）

- **key 来源**：`~/learn-claude-code/.env`，含 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `MODEL_ID`。
- **加载机制**：`config/schema.py::_load_dotenv()` 在 `load_config()` 开头读取该文件并注入环境变量
  （不覆盖 shell 已有值）；可用 `FORGE_ENV_FILE` 环境变量指向其它路径。`default.yaml` 里的
  `${VAR}` 占位符随后被展开。
- **切勿把明文 key 写进 `config/default.yaml`**：该文件已被 git 跟踪，`.gitignore` 的 `config/`
  对已跟踪文件无效，写进去会被提交泄露。密钥只能放仓库外（`~/learn-claude-code/.env`）。
- 当前默认走 `glm-5` + 智谱 anthropic 兼容端点（`https://open.bigmodel.cn/api/anthropic`）。

## 架构地图

| 目录 | 职责 | 关键文件 |
|---|---|---|
| `agent/` | ReAct 主循环与数据结构 | `core.py`（`Agent` 类、`run` 主循环、`_call_with_retry` 重试、stream/complete 分流）、`task.py`（`Task`/`Action`/`ActionType`/`Observation`/`RunResult`）、`event_log.py`（JSONL 事件流，可回放）、`prompt.py` |
| `llm/` | 可插拔 LLM 后端 | `base.py`（`LLMBackend` 抽象 + `stream(on_text, on_thought)`）、`router.py`（`create_backend` 按 provider 选择）、`anthropic_backend.py`、`openai_compat.py`。支持 provider：anthropic / openai / deepseek / groq / ollama |
| `tools/` | agent 可调用的工具 | `base.py`（`BaseTool` + `ToolRegistry` 注册表）、`shell_tool.py`（四层防护：黑名单→白名单→确认→超时）、`file_tool.py`、`search_tool.py`、`test_tool.py`、`git_tool.py`、`runtime.py`（`LocalRuntime` + `DockerRuntime`） |
| `context/` | 上下文管理 | `repo_map.py`（tree-sitter 多语言符号摘要）、`history.py`（滑动窗口）、`token_budget.py`（预算分配，tiktoken 优先） |
| `entry/` | 入口层 | `cli.py`（Click：run / chat / log）、`chat.py`（跨轮持久化）、`github_issue.py`（Issue→PR） |
| `config/` | 配置 | `default.yaml` + `schema.py`（`${VAR}` 展开 + `_load_dotenv` + dataclass 校验） |
| `s20_comprehensive/` | **完整 Harness 参考实现** | `code.py`（2083 行）：Task 系统(70-168)、Worktree(171-273)、MessageBus(472-502)、Hooks/Permission(856-942)、多智能体调度、Cron、MCP。**M1/M2 移植的源头** |
| `tests/` | 测试 | `test_day1~7.py`（渐进式）、`test_chat/confirm/sandbox/stream.py`。当前为同步测试 |

## 开发约定

- Python 3.11+，dataclass + 类型注解，中文注释。
- **新增 provider**：实现 `llm/base.py::LLMBackend`（`complete` + `stream`），在 `llm/router.py` 注册。
  `stream` 必须同时支持 `on_text` 与 `on_thought` 两个回调（思考/正文分离）。
- **新增工具**：继承 `tools/base.py::BaseTool`，注册到 `ToolRegistry`。
- **测试**：当前同步；M1 起引入 async 代码，需补 `pytest-asyncio`（见下方 Roadmap 前置项）。

## 改造 Roadmap（M1→M4，映射到代码现状）

- **M1 核心事务机制**（当前阶段）
  - `task/engine.py`：SQLite(WAL) `TaskEngine`，`tasks` + `task_dependencies` 两表 + DAG 依赖查询。
    迁移自 s20 的 JSON Task 系统（`code.py` 70-168，复用 `claim/can_start/complete` 语义）。
  - `runtime/worktree.py`：async `WorktreeSession`（`__aenter__`/`__aexit__`，`asyncio.create_subprocess_shell`，
    `__aexit__` 强制 `git worktree remove --force`）。复用 s20 的 `validate_worktree_name` 与 `wt/{name}` 分支约定。
  - `scripts/m1_demo.py`：TaskEngine + WorktreeSession 端到端 demo（含异常回滚验证）。
- **M2 Harness 整合**：`ipc/bus.py`（`asyncio.Queue` 的 `AgentBus` pub/sub，替代 s20 文件轮询 MessageBus）；
  `harness/hooks.py` + `harness/permission.py`（移植 s20 Hooks/Permission，与现有 `shell_tool` 四层防护统一为 `ToolExecutor`）。
- **M3 沙箱加固**：在 `DockerRuntime._start_container`(`tools/runtime.py:289`) 的 `run_args` 追加
  `--cpus`/`--memory`/`--read-only`/`/tmp` tmpfs/seccomp；挂载时 worktree 路径 `rw`、主库 `ro`。
- **M4 性能与扩展**：高延迟 I/O 全 async 化；README 预留 Redis Pub/Sub、C++/Tree-sitter 重写接口。

## 已知状态

- 已修复：`llm/anthropic_backend.py::_anthropic_stream` 曾缺 `on_thought` 参数（已补 + `thinking_delta` 事件分发）。
- 待开始：M1 尚未动工；主循环当前同步，M1 起在 I/O 边界引入 asyncio。
