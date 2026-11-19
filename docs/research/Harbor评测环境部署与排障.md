# Forge × Repo2RLEnv：Harbor 评测环境部署与排障

范围：从 Typer 1788 单题接入到 Core-20 的 HTTPX / Click 两仓资格跑。本文按故障层归档，区分已验证的修复、临时绕行和仍待核验的原因。资格跑是 4 题 × A/B 共 8 项，不等于 20 题 × 2 的主实验。

## WSL、磁盘与构件

| 问题 | 处理和验证 |
| --- | --- |
| C 盘空间不足；看到 `/home` 不代表数据在 E 盘 | 先核对 Windows 注册表的发行版 `BasePath` 和 Docker Desktop 的磁盘映像位置。本机此前确认 `Ubuntu-22.04-Recovered` 的 WSL VHDX 位于 `E:\wsl\Ubuntu2204-Recovered`，Docker 数据位于 `E:\Docker\data`。仓库在 `E:\2806\forgeAgent\forge-agent`，实验资产放在 WSL 原生 ext4 的 `~/r2e-workspace`；不要只凭路径推断物理盘。 |
| `/mnt/e` 上 `uv` 锁、原子临时文件和硬链接失败 | `/mnt/e` 是 Windows 挂载，不适合作 Linux 虚拟环境和缓存。这些资产移到 WSL 原生 ext4；Forge 回归用 `~/.venvs/forge-agent/bin/python`，Harbor 用 `~/r2e-workspace/.venv/bin/harbor`。不与 Windows venv 混用。 |
| Windows → WSL 多层引号、PowerShell profile 导致命令失败 | 优先 `wsl.exe -d … --cd … --exec …` 和明确的 Linux 路径；多步操作放在同一侧，不嵌套复杂的变量替换。不把启动命令解析失败当作 Python 或网络故障。 |
| Forge 容器无网，`pip` 在线安装失败或不认改名后的 wheel | 在宿主准备完整 wheelhouse；保留标准 `coding_agent-0.1.0-py3-none-any.whl` 文件名，容器 `--no-index --find-links` 安装。安装 smoke 证明入口可用，不代表解题成功。 |
| `--no-build-isolation` 用旧 setuptools 产出 `UNKNOWN-0.0.0`；旧 venv 的 `TarFile.extractall()` 不支持 `filter=` | 用本地缓存中的现代 setuptools 隔离构建；旧 Python 先逐项验证归档路径和文件类型，再兼容解包。只从 Git 基线加明确列出的源码打包，不从私有配置所在的整棵工作树直接打包。上传目录只放 wheel，校验文件内容、默认配置和 SHA-256，旧构件保留。 |
| 早期 adapter `runtime_env` 作用域错误 | 修正变量作用域；用真实 Harbor 无模型 smoke 验证 `/workspace`、离线安装、CLI 启动和日志回收。准备阶段报错不算有效 trial。 |

## Docker、依赖与任务评分

| 问题 | 处理和验证 |
| --- | --- |
| Docker Desktop 显示已开，但 Harbor 报 daemon 未运行；早期 HTTPX No-op 等 600 秒仍未启动 | 分别检查 WSL 的 Docker server、Compose 和 **Harbor 同环境下的完整 `docker info`**。Harbor 把 `docker info` 非零和 10 秒超时显示为同一句；`docker version` 成功不能替代该检查。第二批在这里阻断，事后检查恢复正常，不能断定当时 Docker 真的关闭。后续用同一 Harbor 原生预检连续通过后再派发；第四批的两次预检为 6.49 秒、2.78 秒。 |
| WSL 拉公共镜像时调用 Windows Docker credential helper 失败 | 给每个公共镜像批次用隔离的空 `DOCKER_CONFIG`（控制命令可用 `--public-images`），不改全局配置；此绕行不适用于需要认证的私有镜像。 |
| 构建中的 GitHub clone 出现 TLS early EOF；依赖下载慢 | 分开定位基础镜像拉取、镜像内 Git clone、PyPI 和数据集下载；它们与运行时模型网关是不同路径。保留构建错误，避免归因成题目或 verifier 失败；复用已核验镜像和宿主 wheelhouse，缺失依赖再按失败层重试。HF Hub 无认证限速曾发生，但数据集最终下载及静态验证完成。没有证据表明一个全局代理能解决所有路径。 |
| 旧 Typer 镜像占盘、Docker 显示容量难判断 | 用 `docker system df -v` 区分镜像的共享层、独占空间和可回收量，用 `docker ps -a` 确认引用。只按已确认的旧 Typer 镜像或容器 ID 定向清理，不做全局 prune；截图的颜色或一个容量数字不能证明某镜像可删或已经回收空间。 |
| HTTPX 3367 / 3412 的 verifier 在 pytest 初始化时崩溃，旧测试又把新版 Click 弃用告警升级为错误 | 用 [兼容副本脚本](../../scripts/prepare_httpx_control.py) 只改副本 Dockerfile：卸掉多余的 `pytest-asyncio`，恢复历史测试依赖，固定 Click 8.1.7。不改冻结提交、题目、测试或评分规则，A/B 用同一兼容副本。 |
| Forge 用户级 CLI 实际测试环境缺 pytest | 确认 `PytestTool` 使用的解释器，在安装依赖补上 pytest；通过独立环境或错误分类把「没装测试工具」和「测试断言失败」分开。不能一律解释为源码没修好或拒绝 FINISH。 |

两仓无模型控制 **8/8**。冻结环境下 No-op → Oracle 的目标测试分别为：HTTPX 3367 `0/13 → 13/13`（回归 `803/803`），HTTPX 3412 `0/1 → 1/1`（回归 `37/37`），Click 3058 `0/2 → 2/2`（回归 `23/23`），Click 3126 `0/4 → 4/4`（回归 `49/49`）。假模型的真实 Harbor relay 演练另有 **8/8**，四个 B trial 均启动并完成过一名假模型 Worker。这两项都不是 Luna 的模型成绩，也没覆盖其余 16 题。

## 模型协议、凭据、预算与重试

| 问题 | 处理和验证 |
| --- | --- |
| 模型 ID、协议或本地配置选择错 | Typer 早期把 GLM 前缀写成 `Qzai-org`，修正为 `zai-org/GLM-5.3-Flash`；Luna 用私有具名 profile 和 `responses` 协议，避免旧 `--config` 盖过正确配置。先做一条文本 smoke，再测原生工具往返；不把能初始化客户端当作网关可用。 |
| 文本伪工具调用或原生工具回传错 | 伪调用的格式错误单独反馈，不误判 FINISH。Responses 原生工具保留原始 `call_id`、工具名与参数，下一轮成对发送 `function_call` / `function_call_output`；裁剪时两者一起保留或丢弃，broker 拒绝错配。直接 SDK smoke 和 Forge→broker 离线都通过，不据此保证每种模型的输出都合规。 |
| 禁网容器到 WSL 的 HTTP relay 探测超时；不能给 Agent 容器 Key | 改成 `docker exec -i` 标准输入输出管道；Key 只经宿主内存文件描述符交给 Harbor，不进容器、命令行或产物。禁网 Docker smoke 已覆盖 Lead/Worker 与凭据隔离。早期 `public` 网络策略下 `--allow-agent-host` 曾被忽略，不把那条路径当作网络隔离证明。 |
| Luna 文本生成能用，却不能可靠地预估输入 | 当前网关的 `responses/input_tokens` 返回 HTTP 200、空响应体；这不代表官方 Responses 或其他路由都不支持。开批探一次并冻结：有可信计数用每 trial 60 万输入 + 输出硬预算；没有则用 `output_only`，128 次请求尝试、单次 8,192 输出、累计 60 万输出；输入事后监控。A/B 同口径，恢复不重新探测；回退不是总 Token 或美元硬上限。 |
| 短 smoke 返回三千多输入 Token；后台显示数字不同 | API 输入含缓存读；后台拆成未命中输入和缓存读。三次合计 `11,030 = 4,246 + 6,784` 输入，另有 64 输出，后台可见实扣 `$0.001063`。客户端未发 Forge 系统提示、Repo Map 或真实工具输出；差额不能归因给 Repo Map，也不能从预算里减掉。服务端为何计入大量输入仍未知。账单费用只用对账实数，缺失用量不补零。 |
| 旧 relay 只报 `trial relay rejected request`，无法定位 | 宿主日志只留请求序号、错误类别、HTTP 状态、异常类型，后来再加底层 transport 类型；容器仍只收通用错误。首批第 6 次错误无可见后台行，不能用后续错误倒推旧原因。 |
| SiliconFlow 的 TPM 429 和 Luna 的网关 429 | 早期 SiliconFlow 不给 `Retry-After` 时，2/4 秒退避无效；历史方案调到 65 秒起步、90 秒上限、10% 抖动。Luna 第三批前 10 次成功、58.7 秒内已知输入 91,345，第 11 次获 429，未证实具体是 TPM、RPM 或其他限制。现方案宿主按 `Retry-After` 等待（缺失默认 60 秒），最多一次并重新扣预算；失败预留不返还，不做 SDK 隐式重试。第四批真实日志已证实等待后能继续，但这不保证每次都恢复。 |
| Click 3058.B 第 26 次报 `APIConnectionError`，无 HTTP 状态 | 前 25 次成功、5 次 429 已恢复；最后一次是连接类异常、不是新的 429，旧日志无法区分建连失败和发送后的断线。新策略只对明确的 `ConnectError` / `ConnectTimeout` 等 5 秒重试一次，和 429 共用一次重试机会且扣同一预算；读取/写入失败或原因未知时不自动重发，避免重复发送可能已送达的请求。仅离线验证，未宣称旧故障已复现或修好。 |

## 验收、超时与恢复

- **先对齐超时，再判结果。** 早期 Typer 试跑出现 Verifier reward 1.0 但 Harbor 外层超时、Forge 无自然终态。外层需给模型、退避、工具、产物收取和独立验收留余量。区分自然 FINISH、资源耗尽、外层超时与基础设施异常；`harbor` 退出码为零或汇总 reward 为零都不足以判断单题。
- **无独立 verifier 就不评分。** 首四个 HTTPX A/B reward 都为 1，其中 3367.B 自身终态是 `recovery_exhausted`，并不抹掉独立验收通过；Click 3058.A reward 0 是有效未通过；Click 3058.B 有 provider_error、无 verifier，不能把 Harbor 摘要的 0 当成评分。无人值守审批只在已核验的隔离内启用，不把禁止高风险命令当作工具完全不可用。
- **Memory 必须跨 trial 且独立验收。** A 无权读写 B；B 候选按仓库隔离，使用封存快照和独立复验后才允许发布给同仓下一项。用验证器专用样例走通过发布和跨容器导入，不等于 Luna 有真实命中。Harbor 销毁后复用基础镜像曾失效，实际验收改用独立 verifier 环境；当前捕获范围只覆盖已跟踪的普通 Python 源文件修改。
- **恢复先看落盘状态。** `plan.json` 是冻结口径；`progress.json` 的 `active` / `blocked` 和 `events.jsonl` 决定进度；`<trial>.log` 是宿主日志，trial 下 `result.json`、`agent/forge/`、`verifier/` 才是验收证据。已评分的 A/B 保留；被阻断批次不直接 `--resume` 或重放，基础设施失败后的补跑单独登记并保留失败证据。停轮询不等于停止后台调度器。

## 当前落盘状态与开跑顺序

`core20-luna-qualify-20261119-04` 冻结 `output_only`。日志里的 **6/8** 是已进入终态的调度项，包含 1 项基础设施故障；实际 **5/8 有评分（4 通过、1 不通过）**。Click 3058.B 无评分，两项 Click 3126 未派发。40 项主实验未启动；不可从不完整配对中宣称完整 Harness 的总体效果。

下一次部署按顺序检查：**E 盘与 WSL venv → Harbor 同配置 Docker 预检 → 干净 wheel 和任务哈希 → No-op/Oracle → 无模型 relay 演练 → 明确授权的模型 smoke 与两仓资格跑 → 确认完整证据后再决定主实验**。具体运行方式见 [Harbor 运行说明](../../examples/harbor/README.md)，早期结果见 [Typer 实验报告](Harbor-Typer-1788实验报告.md)。原始实验产物只在本机 WSL `~/r2e-workspace/experiments/`，不发布凭据、私有配置或原始 Trace。
