# Forge Agent 当前项目状态

> 更新：2026-11-16。此页供新对话快速接续；每项结论均有版本或证据边界。开始开发时先核对实际分支、PR 和源码。

## 本轮 Harness 与 Supervisor-Worker 交付进度

- `pr-test` 第二轮 GLM-5.3-Flash 试跑在 30 Lead 步耗尽预算，独立表达式检查通过，但完整 pytest 为 94 passed、3 failed，未自然完成；未调用 Worker。三次表面 FINISH 实为格式损坏的文本工具调用被错误归类。本轮代码将其改为协议错误反馈；CLI `run` 新增默认自动批准、人工批准和完全授权模式。WSL 定向回归 149 passed，新增边界精简复跑 14 passed，文档一致性 PASS；尚未真实模型复跑。

- `pr-test` 表达式模块首轮实测诊断：GLM-5.3-Flash 在隔离 Worktree 的 18 次工具动作中 16 次允许、2 次需确认但因 CLI 无回调被拒绝；12 次读文件，无编辑、测试或 Worker 委派。第 19 次模型调用时 KeyboardInterrupt，属于中断，不是 50 步失败或任务成功。CLI 已改为交互终端默认连接确认，`python[3] --version` 只读查询可直接放行；107 个相关离线测试通过。尚未复跑真实模型，不能断言任务现在会完成。

- Chat 日常交互：输入 `/` 显示内置命令，输入 `@` 显示目标仓库相对路径；`/model MODEL_ID` 可在当前 provider 上切换模型，私有 YAML 的 `models.NAME` 可切换完整连接配置。选择写入用户目录 `selected-model`，默认配置的下一次 `run`/`chat` 会读取，YAML 注释不会被改写。WSL PTY 已实测命令和文件候选弹出；定向回归 40 passed，文档一致性检查通过。未发送真实模型请求，模型 ID 的远端可用性未验证。

- 即开即用 CLI：新增 `forgeAgent` 命令入口和用户目录配置优先级；`scripts/install_cli.sh` 已在 WSL 安装独立用户级 venv，并从 Forge 仓库外的新终端验证 `forgeAgent run` 提示、包内入口和私有配置解析。`env` 的 `export KEY=...` 写法可自动读取。新环境可从 GitHub 克隆后运行同一脚本；API Key 仍需在新环境配置一次，不能随仓库分发。验证仅覆盖安装、启动、配置解析和 CLI 测试，未调用真实模型。

- 阶段 1 已在 `49de682` 提交并推送至 `origin/dev`：四种 Run Profile、三种 Finish Policy、有效配置展示、日常 pytest 权限策略和有 Token 预算的增量 RepoMap；Runner 独立验收仍保留。吸收了 PR #2 的离线 P0/P1 加固，旧 PR 状态不能代替当前分支状态。
- 阶段 2 已在 `5bd1312` 提交并推送至 `origin/dev`：Lead 通过 `delegate_tasks` 启动 explorer、test_analyst、reviewer 的有界并行只读 Worker；独立 Backend、Registry、Context、Trace 和父子 TaskEngine 状态；超时、取消、部分失败和 Usage 聚合均有离线测试。WSL 定向测试 21 passed；Docker 动态与真实模型尚未验证。
- 阶段 3 `ec984cb` 已快进合入并推送 `origin/dev`：评测入口增加 `supervisor_workers` 和显式总 Token 上限，工具调用统计合并子 Trace。WSL 中 `pr-test` 冻结源码的 12 个参考答案通过预检，两变体各选定 2 个计划 trial、实际 0 个真实模型 trial；付费 A/B 按用户要求等待审查。Windows 隔离环境受 Git/MSYS 命名对象权限错误阻断；完整 WSL 回归为 1270 passed、14 skipped，最终增量定向回归 36 passed，文档构建检查 PASS。

## 当前方向

Forge Agent 作为 Coding Agent 与 Harness/Evaluation 项目继续开发。外部评测目标是 Repo2RLEnv `pr_runtime` 的公开 **100 任务池**；既有调研记录为 13 个仓库、63 个 Python 任务和 37 个 Go 任务。**100 是数据集规模，不是 Forge 已完成的运行数。**任务数量、可评分集合和冻结版本在正式批量实验前须按当前 manifest 复核。

当前已写下的试验协议是 **Python-only**：先验证 `default/tiangolo__typer-1788`，再考虑 12-task Pilot，之后决定是否扩展到全部 Python 任务。它不是完整 100 任务跑分。范围与边界见 [架构审计索引](research/architecture-audit/README.md)及仓库中的 `examples/harbor/README.md`。

## 已有证据与未完成项

- [Qwen3.8-27B Typer 1788 Pilot R1](research/harbor-typer-1788-qwen38-27b-pilot-r1/报告.md)：独立 Verifier reward 为 1.0，但 Harbor 外层 timeout 取消，Forge 缺少自然终态；只能说明该 trial 的代码通过独立验证，不能计作自然 FINISH。
- 2026-11-15 的 GLM-5.3-Flash 单任务重跑在本地交接中仅记录到启动；当前没有核验最终 Harbor job、Trace 与 Verifier，因此状态未知。
- 2026-11-16 本轮开始前，生产代码基线曾为 `dev@35c94c3`，Harness P0/P1 加固仅在独立修复 worktree/PR #2；随后本轮已把该加固代码并入阶段 1 的 `49de682` 并推送。PR #2 的平台状态仍需单独核对。
- `docs/design/` 的原架构审查基线是 2026-11-12 的 `dev@fd700bd`；Agent/Tool 与 Context/RepoMap 两篇另有 11 月 13 日运行期修复补记。后续 Harbor/Harness 修改尚需按当前源码复核受影响专题。

## 下一轮工作入口

1. 核对目标分支和 PR #2 是否合入，按当前源码更新架构事实，不直接沿用旧 P0/P1 TODO。
2. 固定 Repo2RLEnv manifest、Python-only Pilot 任务清单、配置和评分合同；区分任务池规模、实际试验数和可评分数。
3. 核验尚未收口的单任务实验产物，再决定下一轮开发或批量评测。实验路径与本机运行状态放在本地交接，不写入公开状态页。

`docs/design/` 记录已核查的实现架构；`docs/research/` 保存审计与实验；本地 `docs/changes/` 保存逐轮设计、结果与历史交接。历史“下一步”不代表当前优先级。
