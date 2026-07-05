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

> 详细版见 `进度.md`（需 `git add -f`，受 `*.md` 忽略规则影响）。

- **M1 — 事务与状态**：✅ 完成。`task/engine.py`(379L) + `runtime/worktree.py`(295L) + `scripts/m1_demo.py`。提交 `aede64b`。
- **M2 — Harness 与 Bus**：✅ 完成。`ipc/bus.py`(174L) + `harness/{hooks,permission,executor}.py`。提交 `817f92d`。
- **M3 — 沙箱加固**：✅ 完成。`tools/runtime.py`(521L) 加 `build_docker_run_args()` 纯函数 + `mem_limit`/`--cpus`/`network=none`/`--read-only`/`tmpfs` + Worktree rw/ro 白名单；worktree 加 `discard_changes`/`keep`/`count_changes`。提交 `0ecf992`。本机 Docker 已配（systemd+Docker CE，`jfm` 免 sudo）。
- **M4 — 主循环集成**：✅ 完成（第一波核心闭环 `fc9a7d0` + 第二波工程化 `8249678`）。
  - 第一波：`agent/orchestrate.py` async 组合根，`async with WorktreeSession` + `asyncio.to_thread(Agent.run)`。`agent/core.py` 一行未改。safe_path 零侵入（`PermissionManager(workspace=wt.path)`，读写都覆盖）。4 个新 EventType + executor `decision_callback`。`scripts/m4_demo.py` 全断言通过。
  - 第二波：CLI `agent run --isolate [--sandbox]` 直接触发 orchestrator；工具 `default_cwd`（LocalRuntime 下默认在 worktree 内执行）；bus `on_append→queue→forwarder` 转发每条事件到 `events.*`。
- **测试**：全量 **515 passed / 0 failed**。核心模块覆盖率 93%。14 文件 ~3400 行。无 stub/TODO。

> 分支 `feature/s20-integration`。M1-M4 全部完成。

## 接下来要做

### 测试补强（`任务规划.md` Task 4.1）— ✅ 完成
- SQLite 并发读写一致性：`tests/test_task_engine_concurrency.py`（多连接并发 claim，验证 WAL + 原子 UPDATE）。
- Worktree/orchestrate 强制中断（KeyboardInterrupt 模拟）后清理断言：`tests/test_orchestrate.py` 新增中断/失败 result/bus forwarder 健壮性测试。
- 覆盖率：核心模块 **93%**（agent/task/runtime/ipc/harness），全部 ≥86%。
- 已知限制（测试中发现，未修，记 TODO）：① 单个 TaskEngine 实例非线程安全（共享 sqlite connection，多智能体共享时需加锁）；② `create_task` 的 task_id 用 4 位 rand，同秒高频创建会撞 UNIQUE。当前架构不触发，M5+ 多智能体时再处理。

## 关键约束 / 易踩坑

- **不要把 API key 提交进仓库**：key 只在仓库外的 `~/learn-claude-code/.env`。`config/default.yaml` 只放 `${VAR}` 占位。
- **`agent/core.py` 保持同步 ReAct 循环**：已通过 `orchestrate_run`（async 组合根 + `to_thread`）接入，core.py 不改。继续遵守"内核同步、I/O 异步"。
- **Docker 测试本机可跑**：systemd+Docker CE 已配，免 sudo。沙箱逻辑仍建议"参数构造"与"起容器"分离，前者纯单测。
- **async 边界**：内核同步 ReAct，`orchestrate_run` 是唯一 async 组合根，I/O（Worktree/Bus/LLM/Docker）在边界走 asyncio。
- **safe_path 在 executor 层不在工具层**：`PermissionManager(workspace=)` 强制边界，工具零改动，边界由 orchestrator 设（不信 LLM params）。读写都覆盖。
- **`*.md` 被 gitignore**：只留 README/USAGE；新增 .md（如 `进度.md`）要 `git add -f`。
- **易错 API**：`AgentBus.subscribe` 一次一个 topic；Docker CLI 无 `--nano-cpus`（用 `--cpus`）；`event_log` 含下划线的 task_id 别用 filename 解析。
