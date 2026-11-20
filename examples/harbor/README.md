# Harbor 集成

Forge Harbor adapter 在任务容器中安装 Forge wheel，使用生产 CLI 执行任务，并把 Trace 用量和运行终态交回 Harbor。独立 verifier 由 Harbor 组织。

## 准备与冒烟

从仓库根目录构建 wheel，选一个本地 Harbor 任务执行安装与环境检查。

```bash
python -m pip wheel --no-deps --wheel-dir examples/harbor/wheelhouse .
PYTHONPATH="$PWD" harbor run \
  --path /absolute/path/to/task \
  --agent examples.harbor.forge_harbor_agent:ForgeHarborAgent \
  --ak smoke=true \
  --job-name forge-smoke \
  --jobs-dir /absolute/path/to/jobs \
  --n-concurrent 1
```

adapter 从 `r2e_forge_runtime.yaml` 读取示例配置。可以通过 `--ak config_path=PATH` 选用自己的配置，通过 `--ak wheelhouse_path=PATH` 指定独立 wheelhouse。完整运行时移除 `smoke=true` 并配置模型与预算。多任务控制组和计划检查见 `python -m scripts.run_harbor_core20 --help`。

## 实现与效果

`forge_harbor_agent.py` 管环境准备、进入任务和结果回传。`result_policy.py` 判断自然完成、可评分快照和基础设施失败。`trace_evidence.py` 核对实际模型事件、终态与用量。可以使用同一任务及 verifier 比较不同运行配置。[评测设计](../../docs/design/evaluation.md)

## 使用边界

通过环境变量或宿主能力传递凭据。安装冒烟不会发起模型请求。Agent 状态、外部超时和独立 verifier 结果分别读取。运行产物留在 `--jobs-dir` 指向的本地路径。
