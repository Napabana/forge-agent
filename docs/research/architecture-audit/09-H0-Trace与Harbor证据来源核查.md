# 阶段 09 H0：Trace 与 Harbor 独立证据来源核查

> 2026-11-14；基于 `docs/qwen38-27b-pilot-r1` 和真实运行源码 `ca396ce6f14b88dd439bebdcb3b9634645017034`。  
> 状态：源码和真实脱敏归档已检查；新增只读工具和离线测试。**未改 Agent Loop、EvaluationHarness、Harbor Adapter 的生产控制行为，没有调用模型 API。**

## 1. 核心事实

**Harbor 输出的 Token 为 null，不代表模型没有消耗 Token。**

`examples/harbor/forge_harbor_agent.py::run` 先等待 `environment.exec()` 返回，之后才通过 `forge_usage_extract.py` 读取 Trace、将 Token 用量和 metadata 写入 Harbor `AgentContext`。如果 Harbor 外层 watchdog 在 exec 等待过程中超时，后续采集不能保证运行。

实际归档 `harbor-result.sanitized.json` 中 Agent Usage 全部 null，外层 `AgentTimeoutError`，但独立 Verifier 的 reward=1.0。同时 `trace.sanitized.jsonl` 保留 29 次已完成模型调用的 Provider Usage。

因此必须分别记录：真实模型调用的可观测痕迹、Forge 自然终态、Harbor 外层异常、独立 Verifier 结果、计分合同和实际账单。它们不能相互替代。

## 2. 固定源码与数据来源

| 证据 | 真实路径 | 可靠含义 |
|---|---|---|
| 请求开始 | `agent/core.py::Agent.run` 约 375–402 行 | `llm_call_started` 在调用 Provider 之前写入；不能证明已收费 |
| 请求完成 | `agent/core.py::Agent.run` 约 474–494 行 | `llm_call_finished.provider_usage` 是已完成调用的记录 |
| 失败/重试 | `agent/core.py::Agent.run` 约 402–466 行 | 429 retry 不可当作新的成功回复；失败请求的用量未必完整 |
| Trace 关联 | `agent/event_log.py::log_trace` 约 280–352 行 | `run_id`、`model_call_id`、`step_id`、`span_id` |
| 自然终态 | `agent/runner.py::_record_post_run_trace_log` | 正常收口会追加 `run_terminated`，外部中断时可能缺失 |
| Usage Helper | `examples/harbor/forge_usage_extract.py` | 汇总 finished usage；没有成本就返回 null |
| Harbor Adapter | `examples/harbor/forge_harbor_agent.py::run` | 等待执行结束之后才提取 Usage；外层 timeout 可跳过 |
| 独立结果 | `docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/harbor-result.sanitized.json` | Harbor `exception_info` 与独立 `verifier_result` |
| 详细测试 | 同目录 `verifier-reward-details.json` | F2P 1/1、P2P 22/22、parse_status=ok |

## 3. Typer 1788 真实归档检查

使用 GitHub 连接直接读取并解析了 **324 条** Trace 事件，对每个 `model_call_id` 的 start/finish/retry 关联做了检查，结果如下：

| 指标 | 实际值 |
|---|---:|
| `llm_call_started` | 30 |
| `llm_call_finished` | 29 |
| `llm_call_failed` | 0 |
| `llm_call_retry` | 2（Step 22/24 的 429） |
| 未配对请求 | Step 30 |
| `run_terminated` | 0 |
| 已完成调用 input | 849,931 tokens |
| 已完成调用 output | 18,792 tokens |
| 已完成调用 reasoning | 15,264（属于 output 子集） |
| 已完成调用 total | 868,723 tokens |
| Harbor Agent Usage | null |
| Harbor 外部异常 | `AgentTimeoutError` |
| Harbor Verifier | reward=1.0 |

计数、Usage 与归档 `manifest.json.usage_completed_calls_only` 一致。**正确描述是“已知 29 次正常记录的模型调用使用 868,723 Tokens”，不是“所有实际调用只消费 868,723 Tokens”。**第 30 次中途终止，外加失败重试，是否有额外计费需看 Provider 发票。

这次外层 `AgentTimeoutError` 是 Harbor 监控事件；仅凭它不能推断故障归属是 MODEL、PROVIDER 还是 HARNESS。自然 FINISH 没有发生，但独立功能验证通过；其是否进入某项成功率分母，需要先冻结 Benchmark 合同。

## 4. 可执行产物与已做验证

新增：

- `examples/harbor/trace_evidence.py`：**只读、无模型调用**的证据检查器；解析 JSONL、检查单次 Trace 的 run_id、model_call_id、step_id 及孤立/重复事件、部分请求和缺失用量；从 Harbor result 提取 exception、独立 reward。
- `tests/test_trace_evidence.py`：11 个合成数据测试已经在本地运行通过，另有两个用仓库中真实 Typer 归档文件验证 Trace 和 Manifest SHA-256 的测试；**这两项完整归档 Python 回归尚未在当前容器中运行**。
- `examples/harbor/README.md`：修正了“Harbor 外层 timeout ≠ Forge 正常返回”“没有 run_terminated 时也可能有独立 Verifier reward”的旧表述，并加入下方命令。

在 Forge 根目录运行（不依赖 API）：

~~~bash
python -m examples.harbor.trace_evidence \
  --trace docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/trace.sanitized.jsonl \
  --harbor-result docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/harbor-result.sanitized.json \
  --verifier-details docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/verifier-reward-details.json
~~~

此次本地（Python 3.13.5、pytest 9.0.2）把 Stage08/09 的独立原型与新工具合并运行：

~~~text
PYTHONPATH=. python -m pytest -q tests/test_completion_candidate_probe.py tests/test_trial_outcome_probe.py tests/test_trace_evidence.py -k 'not real'
...................................................                      [100%]
51 passed in 0.09s
~~~

这 51 条是**纯离线合成/原型用例**，不是完整 Forge Agent 的真实任务成功率。因当前执行环境无法通过 DNS 克隆仓库，完整 Forge Core、Eval、Harbor 回归以及两个真实归档 Python 用例未在容器内运行；真实归档的关键事件和用量已另外通过 GitHub 连接解析验证。

## 5. 不应提前跳过的生产门槛

- **可信 Usage**：Trace 中只有已完成请求的用量；若存在 pending/failed/retry 或缺失 Provider 字段，工具标记 `usage_is_lower_bound=true`。账单未知时，不能输出成本为零。
- **超时采集**：想修复 Harbor metadata 缺失，需要验证 Harbor 的外层取消之后哪些日志/Artifact 仍可被宿主读取。只加一个 `finally` 并不能保证中止后执行完成。
- **身份绑定**：Forge `run_id` 和 Harbor `trial_id` 不同，目前依赖归档路径/manifest 关联；正式交付前应有可核实的 task/trial/run/source SHA/config hash。
- **评分合同**：本工具有意返回 `scoreable:null`、`failure_domain:null`、`provider_billed_cost:null`，不把 timeout 归咎于模型，也不据 reward 修改自然完成率。
- **生产评测**：`evals/coding_agent/runner.py`、`report.py` 仍有前述试验故障分母和配置漂移问题。本轮不直接改旧历史统计。

## 6. 下一步

1. 在有完整 Forge checkout 的本地离线跑 `python -m pytest -q tests/test_trace_evidence.py tests/test_agent_completion_guards.py tests/test_coding_agent_eval.py tests/test_runner.py tests/test_harbor_examples.py`。
2. Stage 09 H0：确定异常/外层 timeout 后 Trace Artifact 的可靠保留和 Collector 归属，再定义 Trial Result 的可信来源及可评分口径。
3. Stage 09 H1：修正 Eval Factory 与生产 CLI 的有效配置不一致。
4. Stage 03/07：验证 Shell 子进程是否可读 Provider Key，安全隔离完成后才讨论无人值守权限放宽。

**本轮没有实现自动 FINISH、没有放开 Shell、没有降低已测的 Token 消耗，也没有获得真实供应商账单。**
