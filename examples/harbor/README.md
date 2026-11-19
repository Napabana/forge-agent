# Forge Agent × Harbor 示例

本目录给出一个最小 Harbor 0.25 adapter，用于在 Harbor task container 中安装 Forge wheel、执行一次
`agent run`，并把 Trace v2 token/终态映射回 Harbor。

## 安全边界

- API Key 只通过 `SILICONFLOW_API_KEY` 环境变量传递；不要写入 YAML、wheel 或 job artifact。
- `/opt/forge-venv/bin/python` 只运行 Forge；task image 的 `python3` 运行目标仓库 pytest。
- billable model call 前先执行 `cd /workspace && python3 -m pytest --version`。失败按基础设施错误终止。
- Forge `incomplete` 和 `gave_up` 是可交给 verifier 的模型结果。
- Forge `failed`、`canceled` 或缺少可信 Trace 终态按 adapter exception fail closed。
- 原始 Trace、console log、wheel、task checkout 和 secret 文件不应提交到 Git。

## 准备 wheelhouse

从仓库根目录构建 wheel：

```bash
python -m pip wheel --no-deps --wheel-dir examples/harbor/wheelhouse .
```

Harbor adapter 当前期望 wheel 文件名为：

```text
examples/harbor/wheelhouse/coding_agent-0.1.0-py3-none-any.whl
```

`wheelhouse/`、`*.whl` 和运行结果目录应保持未跟踪。

## 配置

示例配置为 `r2e_forge_runtime.yaml`。运行前必须在 host 环境提供：

```bash
export SILICONFLOW_API_KEY='...'
```

如果使用其他 Provider，请复制配置并保持 `api_key: ${ENV_NAME}`，不要写入真实值。

## no-model smoke

下面命令只检查 adapter 安装、container workspace、隐藏路径和目标 pytest 环境，不调用模型：

```bash
PYTHONPATH="$PWD" harbor run \
  --path /absolute/path/to/task \
  --agent examples.harbor.forge_harbor_agent:ForgeHarborAgent \
  --ak smoke=true \
  --job-name forge-harbor-smoke-001 \
  --jobs-dir /absolute/path/to/jobs \
  --n-concurrent 1
```

每次使用新的 `--job-name`，不要覆盖历史 artifact。

## real run

### Core-20 两仓资格预检（无模型）

Core-20 的独立入口在 `scripts/run_harbor_core20.py`。`controls` 不会调用 LLM；下例在仓库根目录运行，
只为 HTTPX 和 Click 各两题生成计划并运行 No-op/Oracle，共 8 个控制 trial。去掉
`--pilot` 则覆盖完整 20 题。输出路径必须是尚不存在的新 campaign：

```bash
~/.venvs/forge-agent/bin/python -m scripts.run_harbor_core20 controls --pilot \
  --selection ../task-selection-20261118/core-20.txt \
  --data ~/r2e-workspace/pr-runtime \
  --campaign ~/r2e-workspace/experiments/core20-pilot-controls-01 \
  --harbor ~/r2e-workspace/.venv/bin/harbor
```

仅查看候选配置、实验预算和 8 次付费 trial 的拟定顺序，用 `plan --pilot --selection ...`。
计划含 `paid_execution_ready: false`；不要把候选配置拿到旧单任务示例里直接调用付费模型。
如果 WSL 在拉公共镜像时无法执行 Docker Desktop 的 Windows 凭据助手，给控制命令加
`--public-images`，脚本仅为当前 campaign 建立空 Docker 配置，不改用户全局配置。

`python -m scripts.run_harbor_core20 rehearse` 使用真实 Harbor 和任务镜像、但只绑定进程内
Responses 替身。它不创建远程 SDK 客户端，不接受供应商凭据，输出也不能当作模型成绩。
使用含 `evals/harbor_offline.py` 的干净 wheelhouse；兼容任务用 `--task-path TASK=PATH`
指定经 No-op/Oracle 验收的副本。队列保存 plan、进度和每次 verifier 结果；未知中断和基础设施
失败不会由 `--resume` 自动重放。禁止从含本地私有配置的工作树直接打包供容器使用。

`rehearse --pilot --relay` 走与付费入口相同的 Docker 标准输入输出管道，但宿主仍只用替身。
管道不依赖 Docker 到 WSL 的网络。Lead、Worker、压缩共用每次 trial 的 600,000 Token 上限，
B 按 480,000 / 108,000 / 12,000 固定分池，不动态借用。每次 trial 最多受理 128 次请求尝试，
每次最多一个计数请求和一个生成请求；禁用 SDK 自动重试。未知用量不退还预留。

Memory 候选只在 B 组捕获，原 trial 独立验收通过后还须用新的 verifier 容器对封存字节复验；
复验后才发布同仓下一轮可读的只读输入包。当前发布只支持已跟踪普通 Python 源文件的修改，
不支持新增、删除、测试或文件模式修改；不能把发布路径已接通当作真实跨任务命中。

`probe-count --execute-paid` 仅发送合成输入的计数请求，可能产生供应商费用，不调用生成。
两仓资格运行需明确授权：在 WSL 导出 `FORGE_EVAL_BASE_URL`、`FORGE_EVAL_API_KEY`；沿用
`rehearse --pilot --relay` 的任务和 wheelhouse 参数，将子命令换成 `qualify --pilot --execute-paid`，
使用新 campaign 目录。开跑前仅探测一次，合同写入 `plan.json`，整批 A/B 同口径，恢复不重探：

- 返回正整数且 `FORGE_EVAL_VERIFIED_COUNT_URL` 与当前地址一致（已核对该端点计数是可信上界）：用原合同，每 trial 600,000 输入 + 输出 Token 硬上限。
- 没拿到可信上界：用回退合同，每 trial 最多 128 次请求尝试，单次最多 8,192 输出 Token，累计 600,000 输出 Token，沿用 A/B 固定分池。输入用量只事后观测，缺失单列；不是总 Token 或美元硬上限。认证、额度、权限或限流错误停止资格运行，不触发回退。

运行中计数失败不会切换合同。调度器从 WSL 环境读取密钥，只通过宿主进程的
一次性内存描述符交接给 Harbor；不进入 Docker 环境、容器、配置、命令行或 campaign 文件。
终端显示开始、耗时和完成数，文件 `progress.json` 与 `events.jsonl` 保留进度。该命令拒绝全量 40 次，
不确定中断和基础设施失败也不会自动重放。
宿主 relay 失败在批次的 `<trial>.log` 中记请求序号、类别、异常类型和 HTTP 状态；不会记录请求体、供应商错误正文或密钥，也不会把这些诊断回传容器。
HTTPX / Click 的 Core-20 relay 另要求 Agent 结束时的包源码指纹与独立 pytest 前后一致，且实际导入自该工作树；证据见 `verifier/forge-source-provenance.json`。缺失或不一致按基础设施异常，不计模型分。原有任务目录、评分脚本和评分输入不被改写；只在私有任务副本启用 verifier 观察器。
付费资格批次的一次生成请求最多重试一次：HTTP 429 采用有效的 `Retry-After`（秒数或日期），缺失或无效时等 60 秒，超过 60 秒或显式额度耗尽则停止；明确的 `ConnectError` / `ConnectTimeout` 等 5 秒。读写错误和原因不明确的连接异常不自动重发。重试占同一个 trial 的请求次数、重新预留对应预算；第一次失败的预留不释放。宿主日志报告等待，`host_input_usage` 分项记录重试。计数接口不使用这项重试，SDK 自动重试仍关闭。

### 旧单任务示例

确认 smoke 成功后，显式设置足够的 agent timeout：

```bash
PYTHONPATH="$PWD" harbor run \
  --path /absolute/path/to/task \
  --agent examples.harbor.forge_harbor_agent:ForgeHarborAgent \
  --ak agent_timeout_sec=5400 \
  --job-name forge-harbor-real-001 \
  --jobs-dir /absolute/path/to/jobs \
  --n-concurrent 1
```

如不从仓库根目录运行，可通过 `--ak` 显式覆盖：

```text
wheelhouse_path=/absolute/path/to/wheelhouse
config_path=/absolute/path/to/r2e_forge_runtime.yaml
usage_helper_path=/absolute/path/to/forge_usage_extract.py
```

## 结果解释

不要只看 Forge CLI exit code：

| Trace v2 终态 | Harbor adapter 行为 | 证据含义 |
|---|---|---|
| `success` | 正常进入 verifier | 可评分模型结果 |
| `incomplete` | 即使 CLI 非零也进入 verifier | 可评分模型结果，如 `max_steps` |
| `gave_up` | 即使 CLI 非零也进入 verifier | 可评分模型结果 |
| `failed` | adapter 非零且不可评分终态时抛异常 | Provider/基础设施故障需单独归因；不能由 adapter 状态推断 Harbor 最终 reward |
| `canceled` | adapter 非零时抛异常 | 标记取消；外部 Verifier 是否执行须以 Harbor 结果为准 |
| 缺少 `run_terminated`，但 adapter 仍获得执行返回 | 默认拒绝当作正常 Forge 终态 | 不能据此宣称 Agent SUCCESS |

完整实验结论见 `docs/research/Harbor-Typer-1788实验报告.md`。


### Harbor 外层超时：单独核对 Trace 和 Verifier

注意：**Harbor 外层超时和 Forge 正常返回后的非零终态，是两条不同控制路径**。如果 Harbor 在 `environment.exec(...)` 等待期间发起超时取消，`forge_harbor_agent.py` 中排在该调用之后的用量提取与 `context.metadata` 更新可能没有机会执行。即使如此，Harbor 的独立 Verifier 仍可能给出非零 reward。

2026-11-13 Typer 1788 / Qwen3.8-27B Pilot R1 就出现了：Forge Trace 30 次 `llm_call_started`、29 次 `llm_call_finished`、没有 `run_terminated`；Harbor 报 `AgentTimeoutError`，Agent Usage 为 null，而 Harbor Verifier 的 `reward=1.0`。**这不是一次自然 FINISH**。不能将 null Token 当成零，也不能把 reward 1.0 改写成 Agent 正常结束。

用只读诊断工具检查已归档的两个原始证据层（不需要 LLM API）：

```bash
python -m examples.harbor.trace_evidence \
  --trace docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/trace.sanitized.jsonl \
  --harbor-result docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/harbor-result.sanitized.json \
  --verifier-details docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/verifier-reward-details.json
```

该命令输出结构化调用计数、Provider Usage、Harbor 异常与奖励，不自动判定 Benchmark `scoreable`。
Forge trial 用量中 `input_tokens` 含 `cached_tokens`（缓存读），`uncached_input_tokens` 为两者之差；缓存读不再计入总量，缺失不作零。这里的未缓存输入指未命中缓存读，如有缓存写仍属于这个差额，不表示按同一价格计费。
`provider_billed_cost` 默认是 `null`；只有将整个 trial 与供应商后台账单核对后，才可传 `--reconciled-billed-cost-usd 金额`。不按 Token 或价格表反推实扣。这个事后分账不改变付费资格或请求预算门禁。
