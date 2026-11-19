# Harness 加固合同

## 执行环境

LocalRuntime 的 Shell/argv、Evaluation command grader、GitHub 独立命令验收使用 `tools/execution_env.py` 的最小环境白名单。Provider Key、自定义凭据、PYTHONPATH、代理认证和 Git 配置变量不会自动传递。LLM Backend 仍在可信宿主进程初始化，环境未被全局修改。

MCP stdio 默认采用相同最小环境；`server.env` 是用户显式授予宿主扩展的配置，可覆盖默认值。授权含义是信任该扩展，不意味着 MCP 自动受到任务 Docker 的限制。

环境白名单只防止直接继承环境变量，不隔离同 UID 进程、宿主文件、凭据文件或 `/proc`。LocalRuntime 仍不适合对恶意仓库无人值守执行。Harbor CLI 本身需要凭据，而同容器同 UID 的任务代码可能访问进程信息；外层 Docker 不能消除这一边界。本轮不声称完整 OS 凭据隔离。

PermissionManager 的显式 `unattended` profile 要求可信调用方提供 `constrained_environment=True`，只增加固定版本查询的授权，并拒绝未知命令、组合命令和环境检查。不会根据 Docker 名字自动开启。LocalRuntime 的产品入口继续使用原有保守权限；无人值守 profile 尚需部署方提供进程/凭据隔离证明后显式装配。文件路径检查解析符号链接，防止 workspace 内链接读取外部文件。

## Completion Candidate

`agent/completion.py` 承接原 completion candidate 研究算法，原 eval 模块保留兼容导出。Core 只根据宿主冻结的既有测试内容、真实 PytestTool 执行和当前仓库内容指纹生成收尾反馈；新建、改写、删除测试与未知参数组合不能升级为可信正向证据。合同中的直接 pytest 命令可以被识别为 contract check，其他命令保持保守。

Candidate 只提示检查 diff、临时文件和诊断失败；不替代 FINISH Guard 或独立验收、不读取 Hidden Verifier、不自动删除文件。窄范围自测通过只给提示；只有当前快照的测试合同或原仓整套测试通过且没有待处理证据才启用硬收尾，快照或未解决证据变化则失效。Trace 单列 `completion_check` 的范围、来源、结果和快照；基础设施/超时不当作测试断言失败。超出有效硬收尾步数产生 INCOMPLETE/resource_exhausted，不自动 SUCCESS。

## Trial 预算

可选 `agent.max_model_calls` 限制逻辑模型请求次数，`agent.max_total_tokens` 在已完成请求后安全边界检查累计 provider usage；默认均关闭。同步请求无法中途硬停，一次请求可能超过限额，HTTP retry 也不等于一个新逻辑请求。用量未知、估算或重试可能导致不完整，启用 token cap 时采取保守终止。单请求 context window、Repo Map budget 和 Trial 累计预算分别控制。

非流式 Responses 的 Harbor 宿主预算在资格批次开始前探测一次并冻结。可信计数可用时预扣输入上界和最大输出；否则固定同一请求次数与输出 Token 硬限额、只事后观测输入。Lead、Worker、语义压缩使用互不借用的固定池；硬限额涉及的未知用量保留全额，SDK 自动重试关闭。不会从任务 YAML 推断上界或在 trial 中降级；回退结果不得声称具备总 Token 硬上限。

生成请求 429 可由宿主按冻结合同等候后重试一次，等待最多 60 秒；只对可辨识的建连失败或建连超时另提供 5 秒等待规则，仍共享同一次重试机会。发送状态不明的读写故障不重发。失败尝试不返还预留；重试重新经过请求计数及预算预留。计数请求不会因此重试或触发中途改合同。

## 评分与兼容性

Evaluation Trial schema v2 保留全部 Trial，分别记录 Agent 状态、Verifier 状态、故障域、模型调用证据、usage 完整性、预冻结评分策略指纹和 scoreability。materialize、factory、grading、snapshot、cleanup 故障独立记录，cleanup 不覆盖原始模型结果。没有模型调用、Provider/Harness 故障或缺少有效 verifier 的行不能伪装为可评分模型失败。

v2 报告按固定 policy 汇总总 Trial、可评分数、成功数和不可评分原因；legacy v1 单独保留原口径。Harbor 外层 timeout 与 reward 可以同时保留；只有显式传入评分合同、故障域及截止快照证据才给出策略评分。

所有验证采用离线 MockBackend、Fake Runtime 或归档数据；机制通过不意味着真实模型成本下降或成功率提高。下一次 A/B 需固定模型/参数、task SHA、依赖、工具清单、权限、context/map/cumulative budgets、timeout、评分 policy 和重复次数，每次只改一个变量。
