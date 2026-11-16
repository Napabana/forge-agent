# 阶段 07：TaskEngine、Worktree、Local/Docker Runtime 与隔离权限横向审查

> 2026-11-14｜`docs/qwen38-27b-pilot-r1`。真实 Pilot 对应的生产源码 `ca396ce6f14b88dd439bebdcb3b9634645017034`。覆盖 F01–F10，共 10 项。**仅源码与现有测试静态审核，未实际启动 Docker/Harbor、未执行攻击 payload。**

## 1. 端到端所有权与实际执行边界

`ExecutionRunner.run` 按 direct / isolate 分流。隔离模式 `agent/orchestrate.py::orchestrate_run` 通过 TaskEngine 认领、WorktreeSession 建独立工作区；如 sandbox=true，构建 DockerRuntime，将 worktree 挂入 `/workspace`，但主进程、LLM Backend、EventLog、MCP ClientManager 和部分控制逻辑仍运行在宿主 Python 进程。普通 direct 模式则按 CLI 装配 Registry/Runtime，不应默认认为所有任务都处于 Docker 沙箱。

Harbor 场景另有一层外部 Docker Environment，`examples/harbor/forge_harbor_agent.py` 在其中运行 Forge CLI。**外层 Docker 环境是否安全不等于 Forge 的内部 `--sandbox` 配置**；两层调度与密钥注入应单独分析。

## 2. 十项完整审计与暂评分

| ID | 范围 | 可定位源码 | 机制与风险 | 静态暂评分 |
|---|---|---|---|---|
| F01 | Task Engine | `task/engine.py::TaskEngine` | SQLite WAL、依赖、owner/status 管理；不等同跨机分布式锁 | 7.5/10（中） |
| F02 | 原子 claim | `task/engine.py::claim_task` | 条件 UPDATE+rowcount 保证数据库内竞争者仅一人认领；事务持有者异常需测试 | 8/10（中高） |
| F03 | Worktree | `runtime/worktree.py::WorktreeSession.create/finalize` | 独立 branch/base commit；保留/删除和 partial artifact 合同 | 7.5/10（中） |
| F04 | LocalRuntime | `tools/runtime.py::LocalRuntime.exec/exec_args` | Shell 走 shell=True，结构化 argv 走 shell=False；默认子进程继承宿主 env | 5/10（高） |
| F05 | Docker sandbox | `tools/runtime.py::build_docker_run_args` | 默认断网、内存/CPU 限额、tmpfs，readonly_root 可选；未见默认 --cap-drop ALL/seccomp 定制 | 6.5/10（高） |
| F06 | Preflight | `tools/runtime.py::DockerRuntime.preflight; agent/orchestrate.py` | 镜像/worktree 可见性先检查，异常区分 preflight | 7/10（中） |
| F07 | direct vs isolate | `entry/cli.py; agent/orchestrate.py` | --sandbox 和 --isolate 组合语义不同；Harbor 里常用 LocalRuntime 但外层 Docker 环境 | 5.5/10（高） |
| F08 | AgentBus | `ipc/bus.py; agent/orchestrate.py::_bus_forwarder` | async queue/event forwarding；同步 Agent.run 在同一 event-loop 里执行 | 6/10（高） |
| F09 | cleanup | `agent/runner.py; agent/orchestrate.py; runtime/worktree.py` | mcp_manager/runtime/worktree 逐层清理，验收前 finalize 时序需明确 | 6/10（中） |
| F10 | 权限与隔离交界 | `harness/permission.py; tools/runtime.py; mcp_integration/manager.py` | CONFIRM 无 callback 拒绝；MCP stdio 通常由宿主进程启动，Docker 隔离不自动覆盖 | 5/10（高） |

按设计2/正确性3、安全可靠性2/证据2/扩展1；暂评分不能替代 Docker 安全测试。

## 3. 最关键 P0 候选：Host/Harbor 内任务代码可能获得 Provider Key

**源码证据链（具备可复核条件）**：

1. `examples/harbor/forge_harbor_agent.py::run` 从 host adapter 读 `SILICONFLOW_API_KEY`，通过 `runtime_env["SILICONFLOW_API_KEY"]` 传给外层 `environment.exec()` 运行的 Forge CLI。
2. Forge 运行时配置 Provider Key 在模型 API Client 所在进程可用。
3. `tools/runtime.py::LocalRuntime.exec` 和 `exec_args` 调用 `subprocess.run` 时没有提供受限的 `env` 映射；通常继承当前进程环境。
4. 对于 Harbor Docker 内 **使用 LocalRuntime 的 Tool**，模型可控制的 Shell/Python/Pytest 代码如果由该进程执行，存在读取其环境中的 Provider Key 的条件风险。是否有外部网络到达还需依 Harbor Docker 网络策略验证，不能无证据宣称已发生泄露。

**这是现有代码数据流支持的高优先级安全风险**。不会通过「Shell 命令可执行」就自动发生泄露；但在确认隔离之前，不能以“降低权限拒绝率”为由广泛 ALLOW 未授权 Shell/Python。应先设计凭据不进入执行面：Provider 请求由单独可信进程/代理完成，面向模型工具的子进程只获得白名单环境变量；再结合容器禁网、只读挂载、非 root、Capabilities/seccomp 等降低风险。具体实现成本/兼容性需测试后确定。

## 4. Docker/Runtime 真实机制和不等价关系

`tools/runtime.py::build_docker_run_args`（约 235–343 行）生成 `--network none`、`--memory`、`--cpus`、`--tmpfs /tmp`；`readonly_root=True` 才加 `--read-only`；`--user` 只有传入 user_uid 才设置；额外 bind mount 默认未指定 ro/rw；Worktree 明确 rw 挂载。因此**不能宣称默认具备完全只读文件系统、非 root 或严格 capability drop**。

`DockerRuntime.exec_args` 不经 `bash -c`，比 `exec` 更适合 Git/Pytest 等结构化参数；`tools/git_tool.py`、`tools/test_tool.py` 当前优先使用 `exec_args`，已不同于 Stage03 旧版本的字符串拼接问题，历史 P0 注入风险**不可照搬到当前生产结论**。但是 `shell` 工具仍会进入 shell 解释器；cwd/路径约束、符号链接、参数派生、敏感信息仍需要独立防御。

`harness/permission.py::PermissionManager._check_command` 把无法证明只读的 Shell 命令设为 CONFIRM，`harness/executor.py::ToolExecutor.execute` 无 callback 时拒绝。本次 Typer R1 的六次 CONFIRM→deny 可复核，但仅说明无人值守 profile 与命令权限不匹配，**不构成应当全允许 Shell 的证据**。

**MCP 正交边界**：`mcp_integration/manager.py::MCPClientManager` 在宿主进程管理 server transport，`MCPToolAdapter` 由 ToolExecutor 间接调用；Forge DockerRuntime 的 namespace/cgroups/禁网不自动覆盖 MCP stdio server。要么将不可信 MCP server 放入同等受限执行环境，要么在文档明确它是有信任前提的宿主扩展。

## 5. 对标 Codex / Pi / Claude 公开能力

- [Codex exec policy](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/exec_policy.rs#L363-L520)、[执行前准备](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/handlers/unified_exec/preparation.rs#L63-L109)：命令分类、审批政策和 sandbox permission 联动；不是“任何 Bash/Python 都能执行”。
- [Pi Agent Bash 工具源码](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/coding-agent/src/core/tools/bash.ts)：单工具执行体验的参考；不应因此推断 Pi Core 自身实现了 Forge 一样的安全边界。
- [Claude Code Hooks/权限文档](https://code.claude.com/docs/en/hooks)：PreToolUse/Stop 等阶段可扩展，不是对其闭源沙箱细节的证明。

**适配结论**：Forge 最应该吸收的是“可执行范围依据真实 sandbox 证明”和“交互式/无人值守 profile 分层”，而非取消所有命令确认。

## 6. 必须先跑的离线/隔离验证矩阵

| ID | 目标 | 安全通过标准 |
|---|---|---|
| EX07-01 | LocalRuntime Tool 子进程的 `os.environ` 键名可见性 | Provider Key 不在非可信执行面环境（只报告是否存在，不打印 Secret） |
| EX07-02 | Harbor adapter → Forge CLI → Runtime 子进程 | 确定 Key 实际传播位置及不可信代码是否接触；不用真实 Token/外联 |
| EX07-03 | Docker `build_docker_run_args` 多种 profile | 检查 network/uid/caps/readonly/mount 组合；默认值与文档一致 |
| EX07-04 | 模型给工具传 cwd 越界、symlink、长命令/组合命令 | 权限边界可解释、越界禁止 |
| EX07-05 | Test Tool/结构化 Git `exec_args` 中带空格/特殊字符 | argv 不经 shell 二次解释、结构化工具不触发未预期命令 |
| EX07-06 | 无交互权限，readonly shell / safe Python diagnostic | 良性命令可用且不扩大密钥/宿主访问范围 |
| EX07-07 | MCP stdio server 在宿主启动/请求 timeout | 资源和副作用隔离、生命周期清理、可信来源定义 |
| EX07-08 | TaskEngine 同时 claim、Worktree retain/discard、异常 cleanup | 所有权不重复、成果不丢失、失败 trace 明确 |
| EX07-09 | 隔离 Agent.run 长时间同步调用 | Bus forwarder 是否实时调度、取消与超时可观察 |
| EX07-10 | 隔离 Worktree finalize 与 hidden verifier | 验收快照有效期定义清楚、discard 后不会读取已删除工作区 |

对照现有测试：`tests/test_task_engine_concurrency.py`、`test_sandbox.py`、`test_execution_workspace.py`、`test_execution_constraints.py`、`test_orchestrate.py`、`test_failure_harness_isolate.py`、`test_mcp_integration.py`。**本轮未运行**。

## 7. 决策、成本与面试问答

- **P0 安全阻塞**：先验证 Key 流向；在 Key 可触及的配置下，不放宽无人值守 Shell/Python。密钥最小暴露+隔离方案设计预计 2–5 人日，实际取决于 Harbor Executor 能否接独立代理。
- **P1**：定义 Sandbox 权限 profile 与审批策略；确保容器和宿主工具各自适用的安全边界（1–3 人日）。
- **P1**：Orchestrator 同步 loop 对 Bus 的调度风险与外部超时下的 Artifact/Trace 清理测试（1–2 人日）。
- **P2**：Worktree isolate+Verifier+discard 的正确时序（1–3 人日），无需全面改 async。
- **保留**：SQLite 原子 claim、Worktree 自定义保留策略、Docker 默认断网/资源限额、exec_args。

面试问“有 Docker 就安全了吗？”答：不够。Docker 配置决定容器内 OS 资源边界，Permission Manager 决定工具可做什么，Provider Key 必须独立保管，MCP 服务若在宿主运行则不受目标任务 Docker 限制；还需考虑未指定 uid/capabilities、可写挂载和网络能力。

**完成边界**：F01–F10 已做静态源码与公开方案对照，未动态证明隔离足够、不声称已实际泄露 API Key。后续 Stage09/11 把该风险作为 unattended 基线准入前置条件。
