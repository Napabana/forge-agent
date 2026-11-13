# Forge Harness 评测与 Python-only 试验实施规划

> 日期：2026-11-13  
> 仓库：\`Napabana/forge-agent\`  
> 分支：\`dev\`  
> 审查基线：\`ac961802ed52f7261299acc66e506e0356a4b1a1\`  
> 范围：文档规划与实验协议；本阶段不修改生产代码，不自动发起真实模型调用。

## 1. 本轮决策

### 1.1 项目定位

Forge Agent 定位为 **Harness Engineering & Evaluation Platform**。重点验证 Agent Loop、Context、Repo Map、Planning、Recovery、工具治理、Runtime、Acceptance 等 Harness 设计是否能在相同模型和任务下产生可测量的收益。

Teacher 轨迹生成、Student SFT、Reward Design、GRPO/OPD 等训练工作放到独立项目，不为了训练项目继续改造 Forge。

### 1.2 Benchmark 范围

本轮 Harbor 外部 Benchmark 采用 **Python-only**：

- 暂不运行 Go 任务，也不要求本轮 Adapter 支持 Go。
- 目标数据集仍为 Harbor 接入的 Repo2RLEnv 真实仓库任务，不替换成 \`pr-test\` 的 12-task 内部回归集。
- 历史 manifest 统计中的 Python 任务数为 63；正式报告前仍须重新核对当前本地 manifest、任务目录和实际可运行任务数。
- \`pr-test\` 继续作为 Forge 内部确定性回归集，不替代外部 Benchmark。

### 1.3 模型与 Provider

首轮模型固定为：

- Model ID：\`Qwen/Qwen3.8-27B\`
- Provider：SiliconFlow
- OpenAI-compatible Base URL：\`https://api.siliconflow.cn/v1\`
- API Key：通过环境变量 \`SILICONFLOW_API_KEY\` 提供，不写入 YAML、日志、文档或提交。
- 用户已确认本地 Forge 配置固定到 Qwen3.8-27B；Codex 执行前仍必须检查**实际传入 Harbor Adapter 的配置文件**，确认最终生效模型没有被 Adapter 默认配置覆盖。

硅基流动官方模型目录将 \`Qwen/Qwen3.8-27B\` 标记为支持 Tools 的模型；官方价格页当前列出的价格为输入 ¥3 / 百万 Token、输出 ¥12 / 百万 Token，缓存命中价格栏为 \`-\`。价格可能变化，正式实验记录应保存查询日期和价格页快照/链接：

- [SiliconFlow 模型目录](https://cloud.siliconflow.cn/open/models?types=chat)
- [SiliconFlow 模型价格](https://siliconflow.cn/pricing)

本规划采用 2026-11-13 查询到的价格做预估，不把预估价格当成账户账单。

## 2. 为什么先做单任务模型对比

不立即启动 12-task 或全部 Python 任务。先使用之前已经跑过的同一个 Harbor 任务：

- Harbor task：\`default/tiangolo__typer-1788\`
- Repository：\`tiangolo/typer\`
- Frozen repository commit：\`959a356fec72cedcfd4ee3f84790789f8e961989\`
- Harbor 版本：历史报告记录为 \`0.25.0\`
- 关键 F2P 测试：\`tests/test_others.py::test_option_uses_envvar_when_required\`
- 缺陷位置：\`typer/core.py::TyperOption.value_from_envvar()\`
- 参考修复：解析出 \`rv\` 后补上 \`return rv\`。
- 该任务元数据标记为 medium，但实际修复是一处单文件、单行返回值缺失；不能将它描述成复杂仓库级任务，也不能代表 63 个 Python 任务的总体难度。

本次只改变模型，尽可能复用原任务、Forge wheel、Agent 配置、工具集、Docker 镜像和预算。先回答：

1. Qwen3.8-27B 能否通过当前 Forge Tool Calling 链路稳定执行？
2. 与 Qwen3.5-4B 的历史运行相比，完成任务需要多少 Steps？
3. Input / Output / Reasoning / Total Tokens 有何变化？
4. 端到端延迟有何变化？
5. Harbor 独立 Verifier 是否通过 F2P/P2P？
6. 按 SiliconFlow 当前价格估算，一次任务实际花费多少？

这只是单任务探索性对照，不构成模型能力排名或统计显著性结论。

## 3. 历史 Qwen3.5-4B 结果与可比性

历史依据：

- \`docs/research/Harbor-Typer-1788实验报告.md\`
- \`docs/research/harbor-typer-1788-summary.json\`

历史公共配置：Qwen3.5-4B、40 max steps、Planning/Recovery/Skills/MCP 关闭。v5/v6/v7 条件存在差异，不能合并成成功率。

| Run | Steps / Calls | Provider Usage | 终态 | 是否可作为模型结果 |
|---|---:|---:|---|---|
| v5 | 27 个成功调用、1 个失败调用 | Total 1,054,388 | SiliconFlow TPM 429 重试耗尽 | 否，Provider failure |
| v6 | 未形成可信终态 | 正确 patch 已生成 | Forge 私有 Python 缺 pytest，随后外部取消 | 否，Harness/环境污染 |
| v7 | 40 steps / 40 个成功 model calls | Input 1,758,740；Output 5,814；Reasoning 3,786；Total 1,764,554；Cached 0 | \`incomplete / resource_exhausted / max_steps\` | 是，一次基础设施干净的模型未完成结果 |

v7 的额外事实：

- pytest preflight 检测到 pytest 9.2.0。
- 专用 Test Tool 在 Step 34 得到 \`47 passed, 1302 deselected\`。
- 仓库最终没有修改。
- 历史 v7 job 没有运行 Harbor verifier；之后对 pristine state 做的等价 no-model verifier 为 F2P 0/1、P2P 22/22、无 regression。该 verifier 结果不能冒充历史 v7 job 内部产物。
- 历史 Trace 的 \`cost_usd\` 为 \`null\`，因此没有可引用的 v7 账户成本。

**本轮对照口径：** v7 是最接近可比的历史参考（基础设施干净、40 steps、模型正常调用），但它是一次 max-steps 未完成样本。v5/v6 只作为 Provider/Harness 故障诊断，不进入模型成功率分母。不能将 v7 的 40 steps、1.764M tokens 说成 Qwen3.5-4B 的稳定平均值。

### 3.1 27B 成本预估

按 v7 的 Token 数量，仅用 Qwen3.8-27B 的现行 SiliconFlow 价格进行假设性估算：

\[
1{,}758{,}740 \times \frac{3}{1{,}000{,}000}
+
5{,}814 \times \frac{12}{1{,}000{,}000}
\approx ¥5.346
\]

这表示“如果 27B 消耗了与 v7 相同数量的输入/输出 Token，按当前标价大约 ¥5.35”，**不是**历史 4B run 的实际费用，也不是本次 27B 的费用上限。

费用计算规则：

- 以 Provider 返回的 \`input_tokens\` 和 \`output_tokens\` 为基础。
- \`reasoning_tokens\` 若属于 \`output_tokens\` 的子集，不得重复计费。
- 记录 \`cached_tokens\`；官方价格页没有给出该模型单独的缓存命中价格，暂不自行假设折扣。
- 同时保存 SiliconFlow 控制台实际账单/用量（若可获取）；Trace 缺少 Provider cost 时，用价格公式标注为估算值。
- 计算公式：\`estimated_cost_cny = input_tokens * 3 / 1_000_000 + output_tokens * 12 / 1_000_000\`。
- 不得仅用 \`total_tokens\` 乘一个统一单价。

当前 \`examples/harbor/forge_usage_extract.py\` 只在 Trace 存在 \`cost_usd\` 时汇总该字段；没有 Provider cost 时会返回 \`null\`。因此本轮必须另外计算人民币估算费用，不能把 \`null\` 当成零成本。

## 4. 首轮试验：Typer 1788 × Qwen3.8-27B

### 4.1 试验设计

| 维度 | 固定要求 |
|---|---|
| Task | \`default/tiangolo__typer-1788\` |
| Repository commit | \`959a356fec72cedcfd4ee3f84790789f8e961989\` |
| Model | \`Qwen/Qwen3.8-27B\` |
| Provider | SiliconFlow OpenAI-compatible API |
| Agent variant | Baseline ReAct |
| Max steps | 40，与 v7 一致 |
| Planning / Recovery / Skills / MCP | 全部关闭 |
| Harbor attempts | 1 |
| Harbor retries | 0 |
| Concurrency | 1 |
| Agent timeout | 5400 秒 |
| Streaming | \`--no-stream\` |
| Environment | Docker，使用同一任务镜像/配置 |
| API Key | 仅从 \`SILICONFLOW_API_KEY\` 环境变量读取 |

除模型外，所有可控配置尽量与 v7 保持一致。若无法复用旧 wheel、旧配置或旧环境，必须记录差异，不得把结果称为严格的 model-only 对照。

### 4.2 运行前检查（不得产生模型调用）

1. 确认本地 task checkout 的 commit 与上述 frozen commit 完全一致。
2. 确认工作区 pristine，隐藏测试路径仍由 Harbor 管理，不被上传给 Agent。
3. 确认 Harbor Docker task 环境可启动，目标仓库 \`python3 -m pytest --version\` 成功。
4. 确认 Harbor host Python 能导入 Adapter。当前 \`examples/harbor/forge_harbor_agent.py\` 使用 \`from typing import override\`，而项目声明 \`requires-python >=3.11\`；Python 3.11 环境需特别验证导入兼容性。若导入失败，先停止，不要消耗模型 Token；本轮只允许做最小 Adapter 兼容修复并记录。
5. 确认 wheelhouse 能在任务容器中离线安装，且 \`/opt/forge-venv/bin/agent --help\` 成功。
6. 确认 \`SILICONFLOW_API_KEY\` 存在，但不得打印值。
7. **确认实际上传的配置文件**：当前 GitHub \`dev@ac961802\` 中，\`config/default.yaml\` 和 \`examples/harbor/r2e_forge_runtime.yaml\` 仍显示 \`Qwen/Qwen3.5-4B\`。Harbor Adapter 默认上传 \`examples/harbor/r2e_forge_runtime.yaml\` 到 \`/tmp/forge-config.yaml\`，并通过 \`agent --config /tmp/forge-config.yaml\` 启动；因此仅修改 \`config/default.yaml\` 不一定影响 Harbor 任务。若本地已固定为 Qwen3.8-27B，确认 Adapter 的 \`config_path\` 指向该文件；否则使用单独的试验配置副本，并通过 Adapter 的 \`config_path\` 参数传入。不要把 API Key 写入配置文件。
8. 运行前输出脱敏后的有效配置摘要：model、provider、base_url、max_steps、max_output_tokens、context budget、Repo Map、planning/recovery/skills/MCP 开关、测试解释器；不输出 api_key。

若以上任一检查不通过，先停在 preflight，不进行真实模型调用。

### 4.3 版本控制与公平性

- 优先复用 v7 对应的 Forge wheel（历史 SHA-256：\`237521f488d541919855f834cfa344e4081744318b8ba7c3e7aaf11d938c167c\`），前提是本地仍有该 wheel 且其 Adapter/运行行为与本轮需要一致。
- 如果 wheel 不存在，必须记录新 wheel 的 SHA-256、构建源码 HEAD 和构建命令。此时本轮与 v7 不再是严格只改变模型的对照。
- 不要因为切换模型顺手更新 Prompt、工具、Repo Map、Context Policy、重试、max_steps 或其他 Harness 行为。
- 每次试验使用新的 Harbor job name 和 jobs 目录子目录，不覆盖 v5/v6/v7 历史产物。

### 4.4 运行参数

本地路径以用户现有工作区为准；历史命令对应：

- Harbor executable：\`/home/jfm/r2e-workspace/.venv/bin/harbor\`
- Task path：\`/home/jfm/r2e-workspace/pr-runtime/tiangolo__typer-1788\`
- Agent：\`forge_harbor_agent:ForgeHarborAgent\`
- Jobs dir：\`/home/jfm/r2e-workspace/experiments/first-python-task/forge-real\`

建议新 job name：\`typer-1788-qwen38-27b-pilot-r1\`。

运行参数必须保持：

\`\`\`bash
harbor run \\
  --path /home/jfm/r2e-workspace/pr-runtime/tiangolo__typer-1788 \\
  --agent forge_harbor_agent:ForgeHarborAgent \\
  --agent-kwarg agent_timeout_sec=5400 \\
  --env docker \\
  --n-concurrent 1 \\
  --n-attempts 1 \\
  --max-retries 0 \\
  --jobs-dir /home/jfm/r2e-workspace/experiments/first-python-task/forge-real \\
  --job-name typer-1788-qwen38-27b-pilot-r1 \\
  --allow-agent-host api.siliconflow.cn \\
  --yes \\
  --no-stream
\`\`\`

如果需要指定模型配置文件，增加 \`--agent-kwarg config_path=/absolute/path/to/qwen38-typer1788.yaml\`；路径必须是实际存在的本地文件。不要把 API Key 作为命令行参数传递。

### 4.5 运行后必须收集的证据

从 Harbor job artifact、Forge Trace v2、console 和 verifier 输出中收集：

| 类别 | 字段 |
|---|---|
| 身份 | task id、repo commit、Harbor version、Forge source HEAD、wheel SHA-256、config SHA-256、model ID |
| 终态 | Forge run_status、termination_reason、resource_reason、Harbor job status、verifier 是否实际执行 |
| 质量 | Harbor reward、F2P passed/total、P2P passed/total、regressions、最终 patch |
| 执行 | steps、finished model calls、failed model calls、tool calls、test attempts、wall time、Harbor elapsed |
| Token | input_tokens、cached_tokens、output_tokens、reasoning_tokens、total_tokens |
| 费用 | Trace provider cost（若有）、按 SiliconFlow 价格计算的 CNY estimate、控制台实际账单（若有） |
| 诊断 | Provider 429、timeout、tool error、pytest 环境错误、completion rejection、max_steps 等事件 |

Harbor 独立 Verifier 是任务结果的主要依据。Forge 自报 \`SUCCESS\` 不足以证明任务通过；若 verifier 没有执行或结果缺失，不得记为任务成功。

## 5. 单任务结果记录模板

完成后在本文档后续追加结果，或创建独立的实验结果报告。不要覆盖历史 v5/v6/v7 结论。

| Model | Run | Steps | Input | Output | Reasoning | Total | Latency | Harbor reward | Termination | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| Qwen3.5-4B | v7 | 40 | 1,758,740 | 5,814 | 3,786 | 1,764,554 | Forge 1296.9s | 历史 job 未执行 verifier | max_steps | 未记录 |
| Qwen3.8-27B | pilot-r1 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |

比较时分别讨论：

- 任务是否通过，而不是只比较 Token。
- 若两者都未通过，比较失败类型、定位/编辑/验证进度，不能只凭 Token 较少判定更好。
- 若 27B 更早完成，确认它通过独立 verifier，而不是提前 FINISH。
- Steps、Tokens、Latency、Cost 都是单次观测值，不代表稳定均值。
- 只有一次 run 时，不计算或宣传 pass@k、总体成功率或统计显著性。

## 6. 后续决策门槛

单任务结果出来后再决定是否扩大评测：

| 结果 | 下一步 |
|---|---|
| Provider / Adapter / Docker / pytest 等基础设施错误 | 不评价模型；修复环境或 Adapter 后重跑同一任务 |
| 27B 正常运行但 max_steps 未完成 | 检查 Trace 与 patch，判断是任务难度、上下文、工具循环还是模型策略问题；必要时对同一任务增加少量重复，不立即扩到 63 个任务 |
| 27B 完成且独立 Verifier 通过，成本可接受 | 进入 12 个代表性 Python 任务 Pilot |
| 27B 完成但成本明显高于预期 | 结合成功质量、Token 和延迟评估是否继续；不因模型规模更大而默认保留 |
| 27B 成功但与 4B 单次结果差异不明确 | 对同一任务增加重复，先估计随机性，再决定是否扩大 |

进入 12-task Pilot 前，至少确认：任务环境稳定、Trace 终态可信、Verifier 正常执行、Token 统计来自 Provider usage、费用计算口径明确。12-task Pilot 通过后再考虑完整 Python 任务集。

## 7. H0–H3 实施路线（单任务 Pilot 之后）

本节保留原 Harness 评测工作，但将 Go 相关工作从当前范围移除。

### H0：Trial 结果分类与成功率分母

**当前源码观察：**

- \`evals/coding_agent/runner.py::_run_trial\` 捕获执行异常后构造 \`RunStatus.FAILED\` 的 \`TrialResult\`，并设置 \`execution_status="executed"\`。
- \`evals/coding_agent/schema.py::TrialResult\` 当前没有独立的 \`scoreable\` / failure category 字段。
- \`evals/coding_agent/report.py::build_report\` 将 \`real_model_executed && evidence_kind == "real_model"\` 的行聚合，并以该组全部行数计算 observed success rate；当前没有独立剔除 Provider/Infrastructure/Cancelled 等不可评分 Trial。
- 因此 H0 必须先于正式成功率报告完成。

**修改目标：**

- 明确区分 gradable model outcome 与 non-gradable execution failure。
- 至少区分模型未完成/错误、Max Steps、Provider 429/网络错误、目标任务环境错误、Harness 自身异常、主动取消、清理失败。
- Trial 记录分类、原始异常、Trace 终态、是否可评分。
- 报告分别输出总 Trial 数、可评分 Trial 数、不可评分分类计数；成功率分母只使用可评分 Trial。
- 不把 Agent 自报 SUCCESS 作为唯一成功标准，独立 Grader/Verifier 优先。

**主要文件：**

- \`evals/coding_agent/runner.py\`
- \`evals/coding_agent/schema.py\`
- \`evals/coding_agent/report.py\`
- 对应的 \`tests/evals\` / \`tests/test_coding_agent_eval*\`（以仓库实际测试文件为准）

**测试与验收：**

- 对 ProviderError、InfrastructureError、HarnessError、Cancelled、MaxSteps、模型终态、Verifier 失败构造确定性用例。
- 断言不可评分 Trial 不进入成功率分母，但仍保留在总数和错误分类统计中。
- 断言 MaxSteps 是可评分的模型未完成结果（前提是 Trace 终态可信且 Verifier 已运行）。
- 断言缺少可信终态或 Verifier artifact 时 fail closed。
- Fake/Scripted 测试不产生模型能力成功率。

**成本：**中等，涉及 schema、runner、report 和回归测试；先保持报告 schema 向后兼容或显式升级版本。

### H1：评测配置与生产入口一致

**当前源码观察：**

- \`evals/coding_agent/__main__.py::_real_runner_factory\` 手工构造 \`AgentConfig\`。
- 当前评测工厂硬编码 \`repo_map_mode="incremental"\`，没有调用 \`entry/context_common.py::build_context_policy\`。
- 生产 CLI 在 \`entry/cli.py\` 中调用 \`build_context_policy(config, backend)\`，并将 Repo Map、测试解释器、Context Policy 等字段注入 AgentConfig。
- 评测配置可能因此与生产 CLI 行为不一致，需先定义评测允许覆盖哪些参数，再复用生产配置构造逻辑。

**修改目标：**

- 抽取生产与评测共用的 AgentConfig / Context Policy 构造入口。
- 评测只能显式覆盖 suite 级任务预算和实验变量；其他配置继承同一生产配置。
- 把 provider、model、tool manifest、Repo Map、Compaction、planning/recovery/skills/MCP、token budget 等最终生效配置写入 run metadata。
- 每次 trial 使用独立 Context Policy 实例，不能跨 trial 共享 checkpoint/usage。

**主要文件：**

- \`evals/coding_agent/__main__.py\`
- \`entry/context_common.py\`
- \`entry/cli.py\`
- \`config/schema.py\`
- \`agent/core.py\`
- \`agent/runner.py\`

**测试与验收：**

- 对相同配置比较生产 CLI 与 Eval factory 的关键 AgentConfig/Context Policy 字段。
- 覆盖 Repo Map mode/budget、Compaction mode/threshold、test Python interpreter、token budget、tool registry。
- 变更实验变量时，metadata 必须明确显示差异。
- 确保每个 trial 的 Context Policy 不共享状态。

**成本：**中高，涉及配置装配重构和多入口回归；不在 H0/H1 前引入新 Harness 功能。

### H2：Python-only Harbor Adapter

**当前源码观察：**

- \`examples/harbor/forge_harbor_agent.py::_preflight\` 当前执行 \`python3 -m pytest --version\`。在 Python-only 范围内这是合理的，不再要求 Go preflight。
- Adapter 使用 \`from typing import override\`；项目声明 Python \`>=3.11\`，因此需在实际 Harbor host Python 3.11 下验证导入兼容性。
- Adapter 默认使用 \`examples/harbor/r2e_forge_runtime.yaml\`，将其上传为 \`/tmp/forge-config.yaml\`；配置中如果仍是 Qwen3.5-4B，就会覆盖用户在其他配置文件中设置的模型。
- \`examples/harbor/forge_usage_extract.py\` 只读取 Trace 已提供的 Provider usage/cost，不会在 \`cost_usd\` 缺失时自动计算人民币价格。
- 离线 wheelhouse 需要在干净 Harbor task image 中验证完整依赖，不以 \`pip wheel --no-deps\` 命令成功作为充分证据。
- \`examples/harbor/result_policy.py\` 允许可信 Trace 的 \`incomplete/gave_up\` 进入 verifier；Provider/基础设施/取消/缺少可信终态应继续 fail closed。

**修改目标：**

- 只保证 Python task 的启动、测试解释器、依赖和 verifier 流程。
- 修复并测试 Python 3.11 Adapter 导入兼容性。
- 明确 Adapter 实际使用的 config_path，并在 metadata 中记录脱敏配置摘要和 SHA-256。
- 确认 wheelhouse 可离线安装所有 Forge 运行依赖。
- 确认 Trace 缺失、截断或无可信 \`run_terminated\` 时不生成模型 reward。
- 保留 Token/Cost 原始字段；成本换算由独立报告层按价格快照计算。

**主要文件：**

- \`examples/harbor/forge_harbor_agent.py\`
- \`examples/harbor/README.md\`
- \`examples/harbor/r2e_forge_runtime.yaml\`
- \`examples/harbor/forge_usage_extract.py\`
- \`examples/harbor/result_policy.py\`
- \`tools/test_tool.py\`
- \`pyproject.toml\`

**测试与验收：**

- Python 3.11/3.12 host import smoke。
- Harbor Docker no-model smoke。
- 干净容器中的离线 wheelhouse 安装。
- pytest preflight 与 Test Tool 使用同一目标环境解释器。
- SUCCESS、INCOMPLETE/MAX_STEPS、GAVE_UP、FAILED、CANCELLED、Trace 缺失分别验证 adapter 行为。
- Harbor verifier 对 gradable terminal outcome 执行，并保存 reward/F2P/P2P/regression artifact。

**成本：**低到中；Go 适配、Go Test Tool 和跨语言抽象全部延期。

### H3：Python 真实任务环境与 Verifier

**修改目标：**

- 先固定 Typer 1788 单任务模型对比，再选取 12 个 Python 任务做 Pilot。
- 12 个任务覆盖多个仓库、难度、单文件/多文件、缺陷修复/功能修改；任务数和分组依据 manifest 实际统计。
- 每个 trial 使用固定 commit、独立 Docker 环境和独立 verifier。
- 保存 Forge Trace、patch、最终仓库状态、Harbor reward、F2P/P2P 与 regression。
- 严格区分模型未完成、Provider failure、task infrastructure failure、Adapter failure、主动取消。

**主要文件：**

- \`examples/harbor/*\`
- \`docs/research/Harbor-Typer-1788实验报告.md\`
- 后续新增 \`docs/research/Harbor-Python-Pilot实验报告.md\`
- 本地 Repo2RLEnv manifest 与 task 目录（路径以实际 workspace 为准）

**测试与验收：**

- no-model smoke 全部通过。
- 12 个任务逐个通过环境 preflight。
- 每个 trial 的模型、配置、任务 commit、wheel SHA 和 job ID 可追溯。
- 独立 verifier 有实际执行记录；缺失时不算成功。
- 报告 overall、按仓库、难度、任务类型的结果，并报告样本量、重复数、Token、Steps、Latency、Cost 和失败分类。

**成本：**中等到高，主要成本来自 Docker task 环境、API 调用和逐任务诊断。先 12 个任务，不直接跑全量。

### 后续：完整 Python Benchmark 与 Harness 消融

在 H0–H3 通过后，才考虑对 manifest 中全部可运行 Python 任务开展实验。每次只改变一个主要 Harness 因素，至少包含 Baseline ReAct、Planning、Recovery、Repo Map、Context Compaction、Skills 等独立实验组。关键任务增加重复，分析模型随机性。不要在一个实验组里同时打开多个新功能后把结果归因给单项功能。

## 8. 本轮不做的事情

- 不运行 Go 任务。
- 不重新寻找其他 Benchmark。
- 不直接启动 12-task 或全量 63-task 的真实模型运行。
- 不在本轮修改 H0/H1 生产评测代码。
- 不因 Qwen3.8-27B 接入而修改 Agent Prompt、Tools、Planning、Recovery、Skills、MCP 或 Context 策略。
- 不把历史 v5/v6/v7 合并计算成功率。
- 不把单次 Typer 1788 结果写成模型总体能力或 Harness 改进结论。
- 不把 Provider 429、环境错误、Adapter 异常或主动取消计为模型失败。

## 9. 本阶段交付与验收

当前阶段交付：

1. 本文档：Python-only 范围、模型固定、Typer 1788 对照协议、H0–H3 分阶段计划。
2. Codex 执行后单独保存一份 Typer 1788 Qwen3.8-27B 运行结果记录；原始 Harbor job、Trace、console、wheel 和 secret 保留在本地，不提交仓库。
3. 只有当单任务 Pilot 的环境、终态、Verifier、Token 和 Cost 证据可信，才进入 12-task Python Pilot。

本轮文档完成不代表模型实验已经执行，也不代表 H0–H3 已修复。真实模型运行必须由用户明确启动，并由用户检查费用和本地 artifacts。
