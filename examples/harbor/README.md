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

该工具只输出结构化调用计数、已完成调用的 Provider Usage、Harbor 异常与奖励；不会计算 Benchmark `scoreable` 或实际账单费用，避免未经实验合同确认就改动成功率分母。测试见 `tests/test_trace_evidence.py`。
