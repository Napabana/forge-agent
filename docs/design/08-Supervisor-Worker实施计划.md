# Forge Harness 与 Supervisor-Worker 实施计划

> 2026-11-16；实施分支 `fix/supervisor-worker-harness-20261116`，起点 `dev@49581a4`。本文是任务步骤，不把计划写成已实现能力。`config/default.yaml` 的用户修改和 `stash@{0}` 保留。

## 目标与顺序

1. **阶段 1，单 Agent Harness**：核对并吸收未合入的 P0/P1 修复；追踪 CLI、Chat、API、Issue 的配置装配；统一运行配置和有效配置展示；实现 `strict`、`balanced`、`advisory` Finish Policy，保留独立验收；按场景收紧或放宽 Shell 授权，并验证凭据隔离；测量和修正 RepoMap 的实际 Token 预算、检索与遥测。运行相关离线测试并提交。
2. **阶段 2，Supervisor-Worker**：沿用同步 `Agent.run()` 和 Runner；建立同一创建路径的独立 Worker Runtime；提供 explorer、test_analyst、reviewer 三种只读角色；主 Agent 通过 `delegate_tasks` 提交多个独立任务。Coordinator 控制并发、单任务及总预算、超时、协作取消、部分结果和父子 Trace；TaskEngine 记录父子状态。Worker 共享只读工作区，Lead 唯一写入。运行并发、隔离、权限与故障测试并提交。
3. **阶段 3，收口**：回归四个入口、Profile、Finish、Permission、RepoMap、Acceptance；运行 `pr-test` 小型 Benchmark Pilot（付费 A/B 留待审查）；补 README、USAGE、设计/源码索引、配置和实验方法与实际结果；运行文档一致性检查。每个阶段确认测试后合并至 `dev` 并推送。

## 已核对的基线与风险

- `ExecutionRunner` 是产品入口的共同合同和独立验收拥有者；`orchestrate_run` 管 isolate Worktree；`Agent.run()` 是同步 ReAct。变更需覆盖四个入口、Runner、isolate 路径、测试替身。
- `Agent` 已有 RepoMap 模式、TokenBudget、查询与遥测；先测实际行为，不直接替换 PageRank。`context` 的 Token 估算和报告、旧消融模式必须保留。
- `PermissionManager`、`ToolExecutor`、`ExecutionConstraints`、ShellTool 和执行环境共同决定权限；仅在 ToolRegistry 删除写工具不足以构成 Worker 只读边界。尤其要覆盖 Shell、Git、MCP 和路径逃逸。
- `fix/harness-audit-remediation-20261115` 与 PR #2 是未合入基线，需核对差异再吸收。用户配置、stash、实验目录、原始 Trace 和密钥不能进入提交。
- 同步 Provider 调用的取消只能在调用边界协作；超时不能宣称强制中断调用。Worker 总预算必须按实际 Usage 计账并阻止继续派发。
- Benchmark 须区分 Agent FINISH、独立验收、超时与未评分；没有真实 A/B 数据时不报告收益。

## 验收证据

阶段日志分别记录变更文件、调用方、测试命令与实际输出。最终以固定代码 SHA 的测试和 Pilot 结果为准；`docs/design/` 与源码索引按最终实现更新。

## 阶段 1 实施记录（离线验收完成）

- 吸收 `fix/harness-audit-remediation-20261115` 的 P0/P1 代码并解决两处设计文档冲突；保留原分支与 PR #2 证据边界。
- `agent.finish_policy` 支持 `strict`、`balanced`、`advisory`。`Task.require_*` 是显式硬要求；CLI/API 的普通测试提及作为推断要求，明确“运行测试”仍是硬要求。FINISH 的成功状态和 Runner 独立验收继续分开；balanced 的未满足推断项进入 warnings，advisory 进入 reports。
- `agent.run_profile` 支持 interactive、safe、benchmark、sandboxed。safe 在 Runner 强制只读并不装配 MCP；benchmark 限制模型调用和总 Token；sandboxed 要求 Docker。CLI 顶层 `--profile` 可覆盖 YAML，`config show` 展示生效配置并遮蔽 key。
- 直接运行的 ToolExecutor 绑定仓库路径 PermissionManager，避免初次 Agent 复用绕过路径约束；可信 interactive 仅自动放行词法受限的 pytest 命令，safe 仍硬拒绝。P0/P1 的不可信子进程环境过滤继续适用。
- 增量 RepoMap 在产品入口默认将初始摘要限制到 2K 实际 Counter tokens，读到目标文件后用 1K 并把路径加入下一次查询；旧 `none/static/query_aware` 模式保留。已有 `find_symbol`、`search_text`、`file_view` 支持按需查定义、引用文本和文件；缓存命中加入 Agent telemetry。尚未通过真实模型消融证明检索质量收益，未引入 PageRank。
- WSL 指定 venv：完整旧套件 1253 passed、14 skipped（在最终摘要与文档修改前启动）；最终阶段 1 定向回归 108 passed、2 warnings；文档一致性检查 PASS。Docker 动态用例属 14 项跳过，真实模型检索质量和付费 A/B 尚未验证。

## 阶段 2 实施记录（离线验证进行中）

- `AgentCoordinator`、`DelegateTasksTool` 和三个只读 Agent Profile 已落地；direct 与 isolate 两条 Runner 路径接线，四入口可通过同一配置启用。Worker Runtime 是新的 Backend、Registry、Executor、History、RepoMap cache、EventLog；TaskEngine 增加父任务/角色/Agent ID 兼容迁移。
- Coordinator 控制有界并发、每 Worker 步数/Token、跨委派总 Token、超时、父取消和部分结果。结构化 Worker 结果包含 Trace、Usage 与完整性标记；Lead 汇总 Worker Usage 后仍执行 Independent Acceptance。同步 Provider 请求只能协作取消，无法强制终止。
- WSL 指定 venv 中 `tests/test_agent_coordinator.py` 与 `tests/test_audit_config_parity.py` 合计 21 passed；含 direct/isolate MockBackend 集成、真实线程并发、权限、失败、超时、取消及评测预算拆分。Docker 使用替身验证构造与清理，动态容器运行尚未验证。

## 阶段 3 实施记录（离线收口完成）

- Evaluation Harness 增加 `supervisor_workers` 变体与 `--total-token-budget`：同一上限下单 Agent 可用全额，Supervisor 为 Lead 预留 2/3、Worker 预留 1/3；Worker 用量与子 Trace 工具调用计入统计，并单列 Worker 非成功数。
- WSL 上以冻结 `pr-test@23019998f2e801e79dea59fd23fc49c58fc20038` 验证 12 个参考答案；已对 `runtime-policy-mode-live`、`power-operation-integration` 分别生成 Single 与 Supervisor 预检记录：各计划 2 trial，实际均为 0，`not_executed`。输出在本机 `evals/results/single-agent-preflight-wsl-20261116/` 与 `evals/results/supervisor-worker-preflight-wsl-20261116/`，不公开原始实验目录。付费 A/B 按用户要求等待审查，尚无成功率、时间或成本对比。
- Windows 隔离环境中的同一预检受到 Git/MSYS 命名对象权限错误阻断；以 WSL 成功记录为准。全量 WSL 回归为 `1270 passed, 14 skipped, 30 warnings`（479.73 秒）；14 项为 Docker 动态跳过。`scripts/check_docs.py --validate --build` 最终 PASS；新增架构页后同步补了构建检查的页面映射。
- 阶段 2 由工作区内同时存在的 `git commit -m test` 提交为 `5bd1312`，检查了提交内容：仅包括已暂存的阶段 2 代码/文档/测试，不含用户配置；已快进推送 `origin/dev`。阶段 3 的评测、文档与后续父任务生命周期修正另行提交。
- 阶段 3 提交 `ec984cb` 已快进合入并推送 `origin/dev`。最终改动后 WSL 受影响测试 36 passed；全量回归 `1270 passed, 14 skipped` 在该提交前的阶段 2 代码上完成，最后的父任务生命周期和指标改动由 36 项定向测试覆盖。实际 Docker 运行、四类正式付费 A/B 和 API 金额仍未执行。
