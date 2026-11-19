# Forge Agent 当前项目状态

> Core-20 评测已冻结，保留原 8 项评分，不派发新的资格跑或 40 项主实验。Worker 角色枚举与执行校验共用定义；窄自测不触发硬收尾。HTTPX / Click relay 评分前交叉核对实际 pytest 导入路径、解释器和源码指纹；来源证据缺失或不一致不计模型分。离线验证不代表真实模型 Worker 或新版 Harbor 评分已经复测。

> 两段 Luna 资格合计 8/8 可评分；HTTPX 4 项通过、Click 4 项未通过，A/B 各 2/4。两项 Click 的 Forge 自报成功但独立 verifier 失败；B 四项委派均因非法角色未启动 Worker，四次 Memory 检索为空。不能据此声称完整 Harness 已资格通过，40 项未启动；本机复盘与 Trace 索引见 `docs/changes/CURRENT.md`。

> 已按用户授权单独补跑 Click 3058.B → 3126.B → 3126.A，保留旧批次已评分的 5 项。补跑冻结 `output_only`，新增明确建连失败的 5 秒一次重试；首次核对已派发 Click 3058.B，验证结果待定。40 次主实验未启动。

> 第四批真实资格有 5/8 可评分（4 通过、1 未通过）；Click 3058.B 第 26 次 `APIConnectionError` 阻断，无 verifier，余 2 项未派发。宿主新增只针对明确建连失败的 5 秒一次重试和脱敏 transport 类型；WSL 80 passed、5 skipped，未重跑。部署与排障问题已归档至 [Harbor 评测环境部署与排障](research/Harbor评测环境部署与排障.md)。

> 经用户授权已用含有界 429 重试的新 wheel 重跑两仓资格（8 trial），两次 Harbor Docker 预检通过；批次冻结 `output_only`。首次已派发 HTTPX 3367.A，成绩尚未核验；未启动 40-trial 主实验。

> 资格批次生成请求 HTTP 429 可在宿主按 `Retry-After`（缺失默认 60 秒）等待后重试一次，重新通过试验请求计数和预算，不返还首次未知预留。规则写入批次计划；WSL 离线 75 passed、5 skipped，干净 wheel 已重建。未启动真实资格复跑。

> 两仓 Luna 资格第三批越过 Docker 预检，HTTPX 3367.A 前 10 次生成成功，第 11 次由宿主诊断确认 HTTP 429；队列阻断、Verifier 未运行、0/8 可评分。根因为当前网关限流，具体计量维度未知；未自动重放。

> 两仓 Luna 资格第二批在 Harbor Docker 预检阶段停止，未创建首个 trial；仅做初始计数探测，没有发生成请求或运行 verifier。事后指定 WSL 的 Docker 探测恢复可用，旧批次第 6 次模型请求故障尚未复现。未自动重试。

> 两仓 Luna 资格批次在 HTTPX 3367.A 的第 6 次模型请求中止；前 5 次成功，Verifier 未运行，0/8 可评分。宿主 relay 已补脱敏诊断（请求序号、类别、异常类型、HTTP 状态），子进程仍只收通用错误；WSL 离线 61 passed、5 skipped。历史第 6 次错误原因不明；未重跑付费资格。

> 本轮经授权启动两仓 Luna 资格批次（8 trial）。干净 wheel 已核验；初始计数没有可用值，整批冻结 `output_only`。首次核对已派发 HTTPX 3367.A，验收结果尚未产生；40-trial 主实验未启动。

> 2026-11-19 两仓资格预算自动选择：首次计数探测取得正计数且端点口径已核对，沿用 60 万总 Token 硬预算；拿不到可信上界则冻结回退为每 trial 128 次请求尝试、单次 8,192、累计 60 万输出 Token，并由宿主记录实际输入与缺失次数。A/B 全批同口径，恢复不重探，中途不切换；鉴权、权限、余额或限流错误中止。指定 WSL 离线回归 66 passed、5 skipped，文档检查 PASS。未请求真实 API 或派发资格试验。

> 2026-11-19 两仓资格门槛复核：Forge 原生 Responses 工具反馈保留原始调用元数据，下一轮成对发给宿主 broker；Agent + broker 用假模型离线往返通过。Luna 单次合成计数探测返回 HTTP 200、响应体 0 字节，未取得可信输入上界；保持每 trial 60 万 Token 硬预算门禁，未启动八次资格或 40 次主实验。

> 2026-11-19 Harbor 用量分账：试验汇总分别记录含缓存的总输入、缓存读、未命中缓存读的输入；Responses/兼容 API 缺少缓存字段不当作零，Lead/Worker/压缩不重复计量。供应商后台核对的实际扣费只能显式录入审计报告。指定 WSL venv 定向离线回归 82 passed、5 skipped；按 Luna 三条后台记录复现 11,030 / 6,784 / 4,246 输入分账，未调用 API，未变更请求预算门禁。

> 2026-11-19 Luna 工具冒烟：指定 WSL venv，现有 Responses 配置，单轮输出上限 128，无重试。直接 SDK 两次 HTTP 200/completed：严格 schema 的合成工具调用通过（4.16 秒，3707/21 输入/输出 Token），携带配对 `function_call` 和 `function_call_output` 的无状态回传通过（2.11 秒，3749/34 Token）。这只验证网关协议，不验证 Forge 序列化、Harbor 或输入计数预算门禁。

> 2026-11-19 Luna 生成冒烟：指定 WSL venv、现有 Luna Responses 档案，单次请求、输出上限 128、无重试；HTTP 200、`completed`、准确返回 `LUNA_SMOKE_OK`。耗时 5.25 秒，网关报告 3,574 输入、9 输出、合计 3,583 Token。只验证文本生成，未验证工具调用或解释异常偏高的输入用量；计数门禁仍未通过，未启动 Harbor A/B。

> 2026-11-19 Luna 单次计数探测：用户授权后在指定 WSL venv 用原 Luna Responses 档案发送合成计数请求，未得到可用计数（退出 ValueError，无 HTTP 状态输出）；离线复核宿主客户端可构造且 SDK 有计数方法。不据此断言网关不支持，不标记计数口径已验证；未重试、未请求生成、未启动两仓 A/B 或主实验。

> 2026-11-19 Core-20 两仓预检：真实 Harbor/Docker 已验证宿主标准输入输出 relay 和独立 Click verifier 的封存快照复验；已接上 B 组受验发布与后续同仓导入。两仓 Oracle/No-op 与假模型均有 8 项通过证据；这不是 Luna 成绩。付费入口只支持显式两仓资格运行；仍须验证供应商计数/计费口径并另行授权。未调用真实 API，未运行 40 项主实验。

> 2026-11-18 Core-20 Memory 导入：通过仓库 ID 检查，在独立空 store 重建已验证记忆和索引；Harbor B 组可在执行前上传并导入，不向下一次传候选或日志。真实 verifier 快照证明及触发发布仍需接线；未运行 Luna。

> 2026-11-18 Core-20 三路预算接线：可信宿主可给 CLI 注入同一 trial 预算，Lead、Worker、语义压缩分别绑定固定池。新增 Responses 输入计数适配，完整透传输入和工具 schema，未知字段时拒绝而不是漏算；仅用本地假传输验证，Luna 网关支持、用量上界与计数费用尚未确认。该注入入口尚未接至 Harbor，API 凭据隔离和跨容器 Memory 仍为付费门槛。

> 2026-11-18 Core-20 请求预算接入：非流式 Responses 支持显式绑定预算池，发送前以完整请求取得可信输入上界并原子预留、限制输出；禁 SDK 自动重试，失败/缺少 usage 不释放预留。四个相关 WSL 测试文件 44 passed（包括真实 SDK + 本地 MockTransport 的 503 单发测试）。两仓无模型控制 8/8 已过；Luna 输入上界、Harbor 三池接线、凭据隔离和跨容器 Memory 尚未端到端验收，未发付费请求。

> 2026-11-18 Core-20 离线门禁：请求预算原子预留和受验 Memory 候选晋升核心逻辑已加定向用例；指定 WSL venv 四个相关测试文件共 44 passed。两个门禁尚未接入真实传输和跨容器快照发布，付费仍关闭。两仓无模型控制可用独立 Docker 配置绕开 WSL 与 Windows 凭据助手冲突。

> 2026-11-18 Core-20 实现首段：功能分支新增两仓 8-trial 与全量 40-trial 计划、Harbor 无模型控制入口、Memory 延迟晋升与 Usage 分账。付费入口未开放：跨容器验证后发布、请求前统一预算与凭据隔离尚未接通；不要从配置开关推断端到端已就绪。

> 2026-11-18 Core-20 设计修订（待审阅）：20 题 × 两组。A 为基础 ReAct，B 正式包含 Multi-Agent 与跨 Trial Project Memory；实际 Worker 调用、统一预算及独立 Verifier 后晋升为正式运行门槛。拟用 Luna，尚未实现、测试或授权运行；详见本机 `docs/changes/2026-11-18/Harbor-Core20-评测实施设计.md`。

> 2026-11-18 外部 Harness 评测选题：已对本地冻结的 Repo2RLEnv 63 个 Python 任务完成静态盘点，筛选覆盖 7 个仓库的 20 个主集任务及 4 个扩展任务，用于固定 45 步的 Harness 配对比较。已记录指令提示、需求歧义和 Typer 既有开发使用等限制；本轮未执行批量模型运行或 Oracle/No-op，不声称 45 步可完成或任何效果提升。清单与文件哈希位置见本机 `docs/changes/CURRENT.md`。

> 下方 2026-11-17 条目中的“当前工作树，未发布”是记录形成时的状态；当前发布状态以分支 Git 历史为准。

> 2026-11-18 Project Memory 增量：已增加本地项目身份、Markdown/JSON/JSONL 存储、候选 Writer、关键词检索、首次请求注入、Lead/Worker 读写边界、Runner 后处理和 CLI 管理命令。新增确定性测试 13 passed；全量离线回归初轮 1316 passed、14 skipped、2 failed（CLI isolate 假结果缺字段），修复后相关测试 31 passed。文档严格构建 PASS。未二次运行完整套件，也未执行真实模型验证；不能声称跨 Session 的实际效果提升。用户 `config/default.yaml` 保持原样。

> 2026-11-17 pr-test 新实测（本地保留 worktree）：安全表达式任务在 26/45 步自然 FINISH，Lead 聚焦测试 7 passed、完整 pytest 75 passed；工作树独立复跑 75 passed、独立验收脚本 PASS，需求主要验收项成立。2 个可选只读 Worker 都在第 4 步耗尽 24K Token 限额，未返回 findings，合计占总用量 13.4%；Skill 各加载一次、MCP 未调用。下一步优先让 Worker 在预算内交付结论；详见本机 `docs/changes/CURRENT.md`。

> 2026-11-17 CLI 资源管理增量（当前工作树，未发布）：新增 `skill add/list`（本地目录或 GitHub Skill 子目录；global/project 两种安装位置）、`mcp add/list`（stdio/HTTP，原位插入统一 YAML 且保留其他值与注释）、`config path/models`（只读）；`config show` 现遮蔽 MCP env 值。离线 CLI/配置/Skill/MCP 定向回归 108 passed，最后新增两条分支测试重跑 13 passed，文档一致性 PASS。用户级 CLI 已更新；实际 Ponytail URL 在临时 project 目录安装/发现成功。未修改用户私有 YAML 或全局 Skill 目录，未调用真实模型；本机证据见交接记录。

> 2026-11-17 提示词校正（当前工作树，未发布）：纯文档或注释改动不再被系统提示词一律要求测试；代码、测试和影响行为的配置仍要求相应验证。WSL 定向 82 passed、文档一致性 PASS。严格完成守卫仍按完整仓库内容指纹判定测试是否过期，因此注释改动后的既有测试可能被要求重跑；本轮未改守卫语义。

> 2026-11-17 增量（当前工作树，未发布）：针对 Luna 43 步自然完成试跑修复 Worker/Skill/Responses 反馈。Worker 只读任务不再继承 Lead 的强制 Plan；已加载 Skill 从可选加载 schema 中移除，重复调用返回明确提示；Responses 文本伪工具调用不再误判为 FINISH；系统提示词补充最后一次修改后的测试要求。WSL 离线定向回归 124 passed，用户级 CLI 已重装；尚未真实模型复跑。本次修改未改变既有试跑的历史终态。

> 更新：2026-11-16。此页供新对话快速接续；每项结论均有版本或证据边界。开始开发时先核对实际分支、PR 和源码。

> 2026-11-17 增量（当前工作树，未发布）：pr-test 全能力试跑实际使用了 Plan、Skill、MCP 和两名 Worker，但 45 步耗尽，两个 Worker 在 12K Token 上限内未返回 findings；Lead 未在最后改动后运行完整测试。事后保留 worktree 的 pytest 为 107 passed，独立检查 PASS，不能算 Agent 自然完成。本轮在源码中补充工具调用格式提示、全角 `Action：`/`Params：` 解析，并将默认 Worker 上限提高为每人 24K、整次 72K；定向离线测试已通过，真实模型尚未复跑。详见本机 `docs/changes/CURRENT.md`。

> 2026-11-17 模型目录增量（当前工作树，未发布）：沿用私有 YAML 的 `models.NAME` 具名配置，`run/chat/config show --model NAME` 现在选中完整模型连接配置，包括该项的 base_url 和 api_key；多个选项可显式引用同一个环境变量。未命中的名称继续只覆盖模型 ID。原 `llm` 不改写。WSL 定向离线测试 108 passed、文档一致性 PASS，用户级 CLI 已重装；尚未用真实 Provider 测试新条目。

> 2026-11-17 统一配置与运行期开关（当前工作树，未发布）：第 1 步 400 的直接原因是命令显式读取无 `models` 的试跑 YAML，裸 `--model gpt-5.6-luna` 被发往其 SiliconFlow 地址。用户级统一配置已有该具名模型及对应地址；Luna 条目已补 `protocol: responses`，Key 引用不变。`run` 新增 `--plan/--no-plan`、`--skills/--no-skills`、`--mcp/--no-mcp`，按次覆盖配置；统一配置保存模型目录和 MCP server 定义，日常 Plan/Skill/MCP 默认为关闭。`--multi-agent` 仍为顶层开关。WSL 定向测试 62 passed、文档检查 PASS，CLI 已重装；真实模型重跑待用户执行。

> 2026-11-17 Luna 试跑测试环境（当前工作树，未发布）：Lead Trace `task_bdca0c3596ef41ae96e33d13dfb28099` 的第 18、30 步 `test` 均因 CLI venv 缺少 pytest 报 `infrastructure`；后续第 34、37 步 FINISH 被 `LATEST_TEST_FAILED` 拒绝，终态是 KeyboardInterrupt，不是自然失败结论。保留 worktree 由 Codex 用开发 venv 完整 pytest 得到 82 passed；这不是 Agent 自己完成的验证。本轮将 pytest 加入 CLI 安装依赖，并区分测试环境错误与真实测试失败；定向离线回归 86 passed，重装后的 `PytestTool` 在该 worktree 返回 success，文档检查 PASS。真实模型尚未复跑。

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
