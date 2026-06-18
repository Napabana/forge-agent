# CLAUDE.md — Forge Agent

> 面向 SWE 的自主编程智能体引擎。融合 `forge-agent`（模块化 ReAct）与 `s20`（多智能体 Harness），
> 目标：强事务一致性 + 底层资源隔离 + 可演示可回放。详见 `改进目标.md`（总目标）与 `任务规划.md`（M1-M4 任务）。

## 环境（已配好，照用）

- **Python venv**：`/home/jfm/.venv`（Python 3.12.13，uv 拉取）。
- **激活**：在 `~/.bashrc` 里 `alias fa='source /home/jfm/.venv/bin/activate && cd /home/jfm/forge-agent'`。新终端敲 `fa` → `agent chat`。
- **`agent` CLI**：已注册（`pyproject.toml` 的 `[project.scripts]`），激活 venv 后直接可用：`agent chat | run | log`。
- **LLM 配置**：`config/default.yaml` 用 `${VAR}` 占位，由 `config/schema.py::_load_dotenv()` 自动从 `~/learn-claude-code/.env` 注入。
  - **切换 provider/端点 = 编辑该 `.env` 的 `MODEL_ID` / `ANTHROPIC_BASE_URL` 段**（取消注释对应行），无需改仓库代码。当前生效：GLM-5。
  - 常见过载错误 `[1305] overloaded_error` 是 z.ai 服务端限流，非配置问题——重试或切端点。
- **跑测试**：`pytest`（venv 内）。需要 Docker 的用例会 skip（本机无 daemon）。

## 架构分层（M1-M4 目标）

| 层 | 职责 | 落地模块 |
|---|---|---|
| 控制内核 | 同步 ReAct 主循环 + I/O 边界异步 | `agent/core.py`, `agent/task.py`, `agent/event_log.py` |
| Task Engine | SQLite(WAL) DAG 任务机 + 事务 | `task/engine.py` (M1) |
| Worktree 事务 | 任务↔代码工作区绑定 + 崩溃回滚 | `runtime/worktree.py` (M1) |
| Agent Bus | 单进程 asyncio.Queue pub/sub | `ipc/bus.py` (M2) |
| Harness | Hooks / Permission / ToolExecutor 拦截管线 | `harness/*.py` (M2) |
| 沙箱运行时 | Docker 资源隔离 + 路径白名单 | `tools/runtime.py` (M3) |
| EventLog | JSONL 审计 + 回放 | `agent/event_log.py` |
| LLM 后端 | anthropic / openai-compat 多后端 | `llm/` |
| 工具 | shell/file/search/test/git | `tools/` |
| 上下文 | repo-map / token budget / history | `context/` |
| 入口 | CLI / Chat / GitHub Issue | `entry/` |

## 当前进度（截至 2026-06-18）

- **M1 — 事务与状态**：✅ 完成。`task/engine.py`(379L) + `runtime/worktree.py`(210L) + `scripts/m1_demo.py`。提交 `aede64b`。
- **M2 — Harness 与 Bus**：✅ 完成。`ipc/bus.py`(174L) + `harness/{hooks,permission,executor}.py`。提交 `817f92d`。
- **测试**：M1/M2 模块测试 102 passed / 7 skipped（Docker）。健康，无 stub/TODO。
- **M3 — 沙箱加固**：⚠️ 部分（~40%）。`tools/runtime.py` 有 `DockerRuntime` 和 `--network none`，但缺 `mem_limit`/`nano_cpus`/`read_only`/`tmpfs` + Worktree rw/ro 白名单。
- **M4 — 集成**：⚠️ 部分（~30%）。测试在，但 **`agent/core.py` 尚未接入 TaskEngine/AgentBus/WorktreeSession**，主循环仍是旧 forge-agent 版本。这是最大的缺口。

> 分支 `feature/s20-integration`。**2026-06-18 wsl 重启丢失的是未提交工作进度，已提交的 M1/M2 代码完好。**

## 接下来要做（按优先级）

### 第 0 步：先提交现有未保存改动
`entry/cli.py`（修复 `input()` 提示符 ANSI 颜色被 readline 吞掉 → `_rl_magenta` + `\001/\002` 包裹）和 `scripts/start.sh`（激活+注入.env 启动脚本）。commit 后再开新工作。

### Step 1 — M3 沙箱加固（`tools/runtime.py`）
目标：把 `DockerRuntime` 的容器启动参数改硬核，满足 `任务规划.md` Task 3.1/3.2。
- 强制注入：`mem_limit`（默认 512m，可配）、`nano_cpus`（单核）、`network_mode='none'`、`read_only=True`、`/tmp` 挂 tmpfs。
- Worktree 白名单：与 `WorktreeSession` 联动，**仅当前任务 worktree 路径 `rw` 挂载**，主库/依赖 `ro`。
- 验证：`tests/test_sandbox.py` 目前 7 个 skip（无 Docker）。要么在有 Docker 的环境跑，要么把"启动参数构造"抽成纯函数单测（不依赖真容器），先保证参数正确性。

### Step 2 — M4 主循环集成（`agent/core.py`，最大块）
把 M1/M2/M3 接进 ReAct 主循环，这是整个合并项目的闭环。
- `TaskEngine`：每次 run 绑定一个 task，`claim_task` → 跑 → `complete_task`/失败标记。
- `WorktreeSession`：`async with` 包住整个 run，隔离代码工作区，崩溃自动回滚。
- `AgentBus` + `Hooks`：工具调用走 `ToolExecutor`（已部分接入），事件上 bus。
- 保留现有 JSONL EventLog，新增 worktree 创建/销毁、权限决策事件。
- 验证：`scripts/` 加一个 M4 端到端 demo（输入 Issue 文本 → 触发完整循环 → 产出 JSONL）。`smoke_test.py` 是参考样板。

### Step 3 — 测试补强（`任务规划.md` Task 4.1）
SQLite 并发读写一致性、Worktree 强制中断（KeyboardInterrupt 模拟）后清理断言、Docker 超载配置校验。核心模块覆盖率 ≥85%。

## 关键约束 / 易踩坑

- **不要把 API key 提交进仓库**：key 只在仓库外的 `~/learn-claude-code/.env`。`config/default.yaml` 只放 `${VAR}` 占位。
- **`agent/core.py` 当前是旧 forge-agent 循环**：集成时别推倒重来，增量接入（先 WorktreeSession 包 run，再 TaskEngine 记账，最后 Bus）。
- **Docker 测试在本机 skip**：写沙箱逻辑时，把"构造容器参数"和"真正起容器"分开，前者可纯单测。
- **async 边界**：内核主循环保持同步 ReAct（清晰可测），I/O 边界（LLM/子进程/Docker）走 `asyncio`/`asyncio.to_thread`。别全局 async 化。
