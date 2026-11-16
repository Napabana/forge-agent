# 阶段 10：Skills、MCP、Trajectory-driven Evolution 与 RL 接口边界

> 2026-11-14｜分支 `docs/qwen38-27b-pilot-r1`，生产源码基线 `ca396ce6f14b88dd439bebdcb3b9634645017034`。覆盖 I01–I11 共 11 项。**源码静态审查+现有测试映射；没有运行真实 MCP Server/付费模型，不开展 SFT/GRPO**。

## 1. 功能边界

Skills 是可发现、按需加载的项目/全局提示与参考资料；MCP 是通过独立 ClientManager 暴露外部服务 Tool 的宿主集成；Trajectory Evolution 是读取验收通过的执行轨迹→挖模式→生成候选 Skill→独立 A/B 与 Promotion Gate→显式批准/部署；这三个方向不等于训练基座模型。

当前项目方向已明确：Forge 作为 Harness Evaluation Platform，**不因名称里有“Evolution”就声称训练了小模型或实现 Agent RL**。I11 保留为接口能力缺口，不推动 GRPO 代码。

## 2. I01–I11 审计表

| ID | 能力 | 来源文件/符号 | 现状与关键边界 | 静态暂评分 |
|---|---|---|---|---|
| I01 | Skills discovery | `skills/catalog.py::SkillCatalog.discover` | 全局+项目目录，项目同名覆盖；frontmatter 校验 | 7.5/10（中高） |
| I02 | metadata-first | `skills/runtime.py::SkillRuntime` | 先列描述，再显式 skill_load/reference_load；不自动执行脚本 | 8/10（中） |
| I03 | 资源读取边界 | `skills/catalog.py::SkillCatalog.read_reference` | 引用清单+目录边界+大小裁剪+symlink 检查 | 7/10（中） |
| I04 | MCP config | `config/schema.py::MCPServerConfig; MCPConfig` | transport/timeout/trust_read_only_annotations 显式配置 | 7/10（中） |
| I05 | MCP Manager 生命周期 | `mcp_integration/manager.py::MCPClientManager` | 宿主线程事件循环，server 顺序 startup/discovery，按 server timeout | 6/10（中） |
| I06 | MCP Adapter | `mcp_integration/adapter.py::MCPToolAdapter` | 命名空间、schema、tool effect、结果截断及超时分型 | 7/10（中） |
| I07 | 权限/Plan 集成 | `agent/core.py; harness/executor.py; mcp_integration/adapter.py` | 非受信只读 annotation 默认 mutation-capable，是否真正隔离仍取决于宿主 | 6/10（中） |
| I08 | 轨迹过滤 | `experience/trajectory.py::load_trajectory` | 需 task_start+run_terminated+success+acceptance passed 才 eligible | 7/10（高） |
| I09 | 候选与存储 | `experience/candidate.py; experience/store.py` | deterministic pattern→candidate、hash/版本与原子文件存储 | 7.5/10（中） |
| I10 | paired Eval/Promotion | `experience/evaluation.py; experience/promotion.py` | required roles+已通过 outcome+触发/非触发+overhead gate+显式 APPROVED | 7.5/10（中高） |
| I11 | RL readiness/数据 | `agent/event_log.py; evals/coding_agent/runner.py; experience/` | Forge 无 SFT/GRPO 生产训练器；Trace 不是可直接用于训练的完整原生轨迹 | 4/10（高） |

## 3. 对标成熟 Agent 的可验证机制

| 范围 | Forge | 外部公开机制 | 取舍 |
|---|---|---|---|
| 扩展工具的执行边界 | MCP ToolAdapter → ToolExecutor/Permission，默认不信任未经授权只读注解 | [Codex exec policy](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/exec_policy.rs#L363-L520) | 外部 Tool 的危险程度需要独立授权+执行隔离，不能靠模型提示词 |
| 单轮调用/Tool 使用 | Forge agent loop 单 Action，MCP tool 映射 ToolRegistry | [Pi agent loop](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L173-L310) | 对照调用生命周期，不把外部 server 标注当可信 |
| Skills 加载 | metadata-first+按需 skill_load/reference_load | [Claude Code 官方 Skills 文档](https://code.claude.com/docs/en/skills) | 关注按需上下文加载，不应默认把全部内容注入 prompt |
| 失败与权限 Hook | 本地 PreHook/Permission/Tool/PostHook | [Claude Code Hooks](https://code.claude.com/docs/en/hooks) | Hook 的存在不证明外部命令已经隔离 |

上述链接仅用于公开可验证机制；没有证明这些产品默认具备与 Forge 相同的 PromotionGate 或 Benchmark 方案。

## 4. 具体优势和风险

**Skills**：`skills/catalog.py::SkillCatalog.discover` 先加载 global 再 project 覆盖同名；`SkillRuntime.render_context` 只列 metadata；`skill_load` 才加载全文，scripts 不自动执行；这一点适合控制常驻上下文。风险是 Skill 文本本身来自仓库，可包含 prompt injection，必须保留作为不可信来源的语义；`skill_load` 的边界不等于信任提升。

**MCP**：`MCPClientManager._supervisor` 对每个 enabled server 顺序启动并在单 server timeout 内列工具；整体 startup limit 使用已启用 server timeout 的 max，而非串行总和（约 `max+5`）；多个 server 顺序启动在慢服务组合下可能被过早判为启动超时。需要启动 deadline 测试，不凭源码推论常态故障。默认 `trust_read_only_annotations=False` 是积极安全设计；但 server 注解一旦被显式信任，read_only_hint 的真实性仍依赖 server，且 MCP stdio server 通常在宿主启动，不进入 Forge DockerRuntime 限制。

**Evolution**：`experience/trajectory.py::load_trajectory` 在缺 `run_terminated` 时拒绝挖掘，即 Typer Pilot 即便 Harbor reward=1 也不会自动成为 eligible 训练/Skill 演化轨迹，这是目前保守而合理的设计。`experience/promotion.py::PromotionGate` 至少要求双来源证据、target/should_trigger/should_not_trigger/non_regression 四角色、实际效果与 Token/Step overhead 限制，`PromotionManager` 还要求已持久化 PASS 决策和显式 APPROVED；应该保留这些判定。

**评测依赖问题**：`experience/evaluation.py::_outcome_success` 用旧 `TrialResult` 的 `run_status/execution_status/acceptance_status` 过滤；若 Stage09 修订失败域与可评分协议，必须同步版本化 Evolution 的输入合同，不能新旧结果静默混用。

## 5. 必测矩阵

| ID | 反例 | 预期 |
|---|---|---|
| SK10-01 | 项目 Skill 覆盖全局同名、frontmatter 错误、未知引用/越界 symlink | 结果与错误明确，拒绝隐式执行 script |
| SK10-02 | 有 50 个 Skills 时未选择任何 Skill | 只注入 metadata，且 Token 上限可测 |
| MCP10-01 | 三个逐个接近 timeout 的 server 顺序启动 | 检查 global startup deadline 与单 server deadline 冲突 |
| MCP10-02 | server 注解同时 read_only/destructive | fail-closed；trusted 标注不是不可推翻的事实 |
| MCP10-03 | MCP timeout 后服务端仍完成有副作用操作 | 不因本地 timeout 就认为无副作用 |
| MCP10-04 | host stdio MCP 进程访问 Key/工作区/外网 | 与 Stage07 凭据及隔离合同一致，不默认套用 Docker sandbox |
| EVO10-01 | Trace 无 terminal、接受未确认、试验分数仅来自外部 timeout verifier | 不被错误纳入 eligible mining |
| EVO10-02 | 候选版本/hash/评测结果不一致或应阻止触发的任务被加载 | Promotion Gate reject、不可覆盖用户管理 Skill |
| EVO10-03 | Baseline 成功 Candidate 失败、Token/Step 超额 | 不晋升，记录完整证据/原因 |
| EVO10-04 | 从 TrialResult 导出模型训练轨迹 | 明确历史文本化 Observation/ToolID 缺口，当前不生成训练奖励数据 |

相关现有测试：`tests/test_agent_skills.py`、`test_mcp_integration.py`、`test_skill_evolution.py`、`test_real_skill_evolution_eval.py`、`test_real_skill_evolution_driver.py`。**本轮未实际运行**。

## 6. 决策、成本和面试问答

- **P1（安全）**：MCP Server 与任务工作区、Provider Key 的边界，以及顺序启动 deadline 测试（1–3 人日）。
- **P1（成本）**：Skills metadata/按需加载对 Prompt Token 的增量测量；不要一次加载所有 Skills。
- **P2（研究）**：Evolution 只有在可靠 scoreability/failure-domain 和同模型配对评测之后才可审计晋升效果，继续保留人工批准、绝不自动部署未经认可的候选。
- **P3**：不在 Forge 上实现 SFT/GRPO；若未来做后训练，另建明确的数据清洗/版本/授权和 RL Env 工程。

面试问“Forge 有没有自进化？”答：有基于**轨迹挖掘→候选 Skill→配对评估→PromotionGate** 的规则化能力增量与回滚，未实现模型参数更新。面试问“MCP read-only Tool 是否绝对安全？”答：不安全。注解来自 server，进程可能在宿主执行，必须结合信任配置、权限策略和真实执行隔离。

**完成边界**：I01–I11 的源码/测试/横向机制与风险核对已覆盖；没有运行 MCP 服务器验证，也没有把 Evolution 结果改称 RL 训练成效。最终阶段 11 统一决定哪些能力构成准入基线。
