# Harbor × Forge：Typer 1788 实验报告

## 结论

在 Repo2RLEnv/Harbor 任务 `default/tiangolo__typer-1788` 上，三轮真实模型运行不能简单合并为
“模型成功率”数据：

| Run | Forge/Harbor 结果 | 仓库结果 | 归因 |
|---|---|---|---|
| v5 | `FAILED / provider_error` | 无修改 | SiliconFlow TPM 429 重试耗尽，不可作为模型 reward 失败 |
| v6 | Harbor `CancelledError`，reward/verifier 均为空 | 得到正确单行 patch | Test Tool 使用 Forge 私有 venv，缺少 pytest；随后外部取消，轨迹受 Harness 污染 |
| v7 | Forge `INCOMPLETE / resource_exhausted / max_steps` | 无修改 | pytest 与 Provider retry 均正常，是本组首个基础设施干净的模型失败 |

v6 证明模型能够定位并修复该问题；v7 证明同一小模型轨迹并不稳定。当前证据支持先构造高质量 SFT
轨迹，不能从这三个不同条件的 run 计算成功率，也不支持直接启动 GRPO。

## 固定任务事实

- Harbor：0.25.0。
- Task：`default/tiangolo__typer-1788`。
- Frozen repository ref：`959a356fec72cedcfd4ee3f84790789f8e961989`。
- 模型：`Qwen/Qwen3.5-4B`，OpenAI-compatible SiliconFlow endpoint。
- Agent budget：40 steps；Planning、Recovery、Skills、MCP 均关闭。
- F2P：`tests/test_others.py::test_option_uses_envvar_when_required`。
- 实际缺陷位于 `typer/core.py::TyperOption.value_from_envvar()`：解析出 `rv` 后缺少 `return rv`。

该任务虽然元数据标为 medium，但实际修复是一处单文件、单行返回值缺失，不能代表一般中等难度 coding task。

## v5：Provider failure

v5 在 Step 28 的模型请求处遭遇 SiliconFlow TPM 429。wheel 中仍使用默认 2 秒、4 秒快速重试，
无法覆盖 Provider 的分钟级 token window，最终 Trace v2 为：

```text
run_status=failed
termination_reason=provider_error
```

仓库无修改。累计 27 个成功 model call、1 个失败 call；Provider usage 为：

```text
input_tokens=1,048,565
output_tokens=5,823
reasoning_tokens=4,282
total_tokens=1,054,388
```

因此 v5 是 Provider failure，不是 reward=0 的模型能力样本。

## v6：正确 patch，Harness 污染

v6 wheel 恢复了 65 秒初始退避、90 秒上限和 10% jitter，真实等待约 66.2 秒与 67.0 秒后继续执行。
模型最终产生了正确 patch：

```python
rv = self.resolve_envvar_value(ctx)
if rv is None:
    return None
return rv
```

随后 Test Tool 错误使用 `/opt/forge-venv/bin/python`，得到：

```text
No module named pytest
```

目标 task image 的项目依赖和 pytest 实际安装在系统 `python3`。模型后续尝试通过 shell/pip 绕过，均被权限策略拒绝，
最终持久化 Harbor 状态为 `CancelledError`，reward 和 verifier 均为空。

v6 的 locate/edit 片段可以人工整理为 SFT expert trajectory，但取消后的整个 trial 不能作为 reward 样本，也不能当作自然模型终态。

## v7：基础设施干净的模型失败

v7 修复了 Test Tool 解释器与 pytest failure classification，并在 billable model call 前执行：

```bash
cd /workspace && python3 -m pytest --version
```

运行中：

- preflight 检测到 pytest 9.2.0；
- 专用 Test Tool 在 Step 34 得到 `47 passed, 1302 deselected`；
- 四次 TPM 429 分别通过约 65.8、70.9、68.2、65.3 秒等待恢复；
- 40 个 model call 完成，0 个 model call 失败；
- Step 26、28 的提前 FINISH 被 Completion Guard 以 `REPOSITORY_UNCHANGED` 拒绝；
- 最终耗尽 40 steps，未修改仓库。

Trace v2 终态：

```text
run_status=incomplete
termination_reason=resource_exhausted
resource_reason=max_steps
```

usage 与耗时：

```text
input_tokens=1,758,740
output_tokens=5,814
reasoning_tokens=3,786
total_tokens=1,764,554
cache_tokens=0
Forge elapsed≈1296.9s
Harbor elapsed≈22m24s
```

Provider 未报告 cost。

旧 adapter 把 Forge CLI 非零退出码一律转为 `RuntimeError`，导致历史 v7 job 未运行 verifier。仓库最终状态完全未变；
同一 pristine state 的 no-model smoke verifier 得到 reward 0.0、F2P 0/1、P2P 22/22、无 regression。
这些值是确定性的等价仓库状态结果，不是历史 v7 trial 内部生成的 verifier artifact，不能混淆 provenance。

## 修复

### Provider retry

`AgentConfig` 默认值调整为：

```text
llm_retry_delay=65.0
llm_retry_max_delay=90.0
llm_retry_jitter=0.1
```

这只改善无 `Retry-After` 的分钟级 TPM 429 生存能力，不保证 Provider 可用性。

### 测试解释器与错误分类

- `/opt/forge-venv/bin/python`：运行 Forge 本身。
- task image `python3`：运行目标项目 pytest。
- pytest assertion failure / no tests collected：`TOOL_EXECUTION`。
- pytest/解释器缺失、internal/usage error：`INFRASTRUCTURE`。
- timeout：`TIMEOUT`。

没有放宽 Shell permission。

### Harbor result mapping

adapter.3 依据可信 Trace v2 `run_terminated` 分类，而不是只依据 CLI exit code：

- `incomplete`、`gave_up`：允许 Harbor verifier 评分。
- `failed`、`canceled`、缺少终态：抛 adapter exception，避免把基础设施问题写成 reward=0。

## Artifact identity

三个 wheel 使用相同包名/版本，必须用 SHA-256 区分：

| wheel | SHA-256 |
|---|---|
| v5 | `c57256821ad674c4e9b280b5790ca453c7c149078b0ed6108bb94d71e0b359a5` |
| v6 | `bdd57931c7d99dadc7704b502d64a93d329a93a2dffec9d82e60b77111ef67cb` |
| v7 | `237521f488d541919855f834cfa344e4081744318b8ba7c3e7aaf11d938c167c` |

wheel、完整 Trace、console log、task checkout 与 secret 文件只保留在本地，不进入 Git。

## 训练建议

优先用 v6 的正确路径构造短 SFT 轨迹：

```text
读取失败意图 → 搜索 value_from_envvar → 识别缺失 return → 单行编辑 → focused test → FINISH
```

v7 可作为“搜索/编辑效率不足”的诊断案例，但不应仅凭一次 reward=0 直接用于 GRPO。进入 preference optimization 或
Agent RL 前，至少应积累多仓库、基础设施干净、终态可信、包含成功与失败差异的样本，并按 repository 划分 train/dev/test。

## 证据边界

可以说：

- v5 证明慢退避对本次 TPM window 是必要的；
- v6 证明模型至少一次找到了正确修复；
- v7 是一次基础设施干净的 max-steps 模型失败；
- adapter.3 能把 gradable terminal outcome 与 Provider/基础设施 failure 分开。

不能说：

- 不能把 v5/v6/v7 合并计算模型成功率；
- 不能把 v6 的取消结果当作 reward；
- 不能把 smoke verifier 冒充为历史 v7 内部 verifier；
- 不能从单任务推断 Qwen 4B 的总体 coding 能力；
- 不能宣称慢退避解决了所有 Provider 可靠性问题。
