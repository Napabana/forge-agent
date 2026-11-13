# 阶段 09：Trace、Evaluation 与 Benchmark 可信性（H0/H1 初审）

> 日期：2026-11-14。状态：**关键路径静态源码初审；尚未完成 H01–H10 全量审计及外部横向对标**。不修改生产代码、不调用模型。
> 生产源码基线：`ca396ce6f14b88dd439bebdcb3b9634645017034`；工作分支 `docs/qwen38-27b-pilot-r1`。

## 1. 结论

**E09-01：Real-model 异常可能错误进入模型成功率分母。**

- `evals/coding_agent/runner.py::EvaluationHarness._run_trial` 在 `except Exception` 中把 Runner 异常包装为 `RunStatus.FAILED`、`termination_reason="evaluation_runner_exception"`。
- 后续仍构造 `TrialResult(execution_status="executed", real_model_executed=self.real_model_executed)`。
- `evals/coding_agent/schema.py::TrialResult` 没有 `scoreable`、`failure_domain`、`verifier_executed` 字段。
- `evals/coding_agent/report.py::build_report` 根据 `real_model_executed and evidence_kind=="real_model"` 聚合，并以 `len(group)` 作为 observed success rate 分母。
- 因此 Provider、Harness、环境故障发生于执行中时，**可能被统计为 real-model 一次失败**。这是源码可证明的路径，但本轮尚未执行该异常注入测试。

**E09-02：评测入口与生产入口的有效配置有明确差异。**

- `evals/coding_agent/__main__.py::_real_runner_factory` 手工构造 `AgentConfig`，硬编码 `repo_map_mode="incremental"`，没有显式传 `repo_map_budget`、`test_python_executable` 或 `prepare_next_turn`。
- `entry/cli.py::run` 调用了 `entry/context_common.py::build_context_policy`，同时注入 Repo Map 预算、Test Python 路径及 Context Policy。
- 因此“相同模型+相同任务”不保证两个入口执行的是同一个 Harness 配置，必须报告实际生效配置并定义可修改的实验变量。

**必须保留的边界**：

- `evals/coding_agent/runner.py` 中，如果 Runner 正常返回但 Agent 的 `RunStatus` 不是 SUCCESS，仍执行 `cached_outcome or grade_many(...)`；它可以记录独立 grader PASS，但 `TrialResult.success=False`。它不等价于 `agent/runner.py::_apply_independent_acceptance`（后者只有 Agent SUCCESS 才运行 hidden verifier）。
- Harbor 的 `reward=1.0 + AgentTimeoutError` 可同时成立。不得为了模型成绩把 timeout 改写为自然 SUCCESS；也不得未经事前冻结评分协议就把所有 timeout 剔除。
- `evals/coding_agent/report.py` 不对 Fake/Scripted trial 发布能力成功率，这是正确实现，应保留。

## 2. 源码证据表

| 主题 | Forge 路径 | 结论 |
|---|---|---|
| 数据模型 | `evals/coding_agent/schema.py::TrialResult` 306–333 | 没有 scoreable 与 failure domain |
| 异常处理与二次 grader | `evals/coding_agent/runner.py::_run_trial` 292–412 | 异常可能转真实模型失败行 |
| 成功率聚合 | `evals/coding_agent/report.py::build_report` 16–54 | real rows 的全部组员参与分母 |
| Eval 配置工厂 | `evals/coding_agent/__main__.py::_real_runner_factory` 106–144 | Repo Map/Context 与 CLI 不一致 |
| CLI 配置 | `entry/cli.py::run` 290–362 | 显式构造 Context Policy |
| Trace 终态 | `agent/event_log.py::log_run_termination` 436–480 | status/reason/error 单独记录 |
| 过程计数 | `evals/coding_agent/graders.py::extract_metrics` 314–412 | Tool、Test、Planning、Recovery、Skills/MCP 指标 |
| Harbor | `examples/harbor/result_policy.py` | `incomplete`/`gave_up` 可视为非零但可评分终态 |
| 真实案例 | `docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/报告.md` | reward=1.0、没有 FINISH、外层 timeout |

## 3. 其他应核查的风险

| ID | 当前源码行为 | 是否经过新动态测试 |
|---|---|---|
| E09-03 | `materialize_task_repo`、`runner_factory` 在 `runner._run_trial` 的 try 之外，异常可能中断整个 suite，未必有 TrialResult | 否 |
| E09-04 | `runner.close` 异常会覆盖原始 `run_result.status/termination_reason/error`，导致模型结果和清理结果混合 | 否 |
| E09-05 | `success` 必须 Agent SUCCESS + 允许的 acceptance + required grader pass，合理保守但不同于“代码最终被独立评分解决” | 仅静态 |
| E09-06 | `TrialMetrics` 包含 Provider Usage 和多类过程计数，应保留；价格估算须按 input/output/cached 等分开计算 | 仅静态 |
| E09-07 | Fake/Scripted trial 的能力成功率被故意省略 | 有现成测试，未于本轮执行 |
| E09-08 | `load_trace_events` 遇 JSONL 无法解析行会跳过；过程指标可能无证据，需要区分“值为零”和“Trace 不可信” | 否 |
| E09-09 | `TrialResult.execution_status="executed"` 为硬编码，不能证明 Provider 已实际完成任何调用 | 否 |
| E09-10 | `EvalTask.test_cmd` 会传入 Task，但当前 Completion Guard 不强制运行指定的 test_cmd | 仅静态 |

## 4. 建议的多轴 Trial 结果协议（待设计/实现）

| 字段 | 示例 | 原则 |
|---|---|---|
| failure_domain | none / model / provider / environment / harness / canceled | 不能靠单个布尔 success 推导 |
| rollout_status | finish / incomplete / gave_up / failed / canceled / timeout / unknown | 与独立 grader 结果分开 |
| verifier_executed / verifier_passed | bool / bool-or-null | 不执行不同于执行失败 |
| scoreable | bool | 由**实验开始前**冻结的 Benchmark Contract 判定 |
| trial_phase | preparing / running / grading / cleanup / complete | 有助于定位异常发生位置 |
| usage | input/output/reasoning/cached, cost estimate | 不重复计算 reasoning，实际账单和预估区分 |
| identity | model/provider/config hash/repo commit/tool manifest | A/B 需冻结有效配置 |

**参考真值表**：

| Agent rollout | Verifier | 解释 |
|---|---|---|
| FINISH | PASS | 自然完成且功能通过 |
| FINISH | FAIL | 模型宣称完成，但功能未解决 |
| INCOMPLETE | PASS | 未自然完成，最终代码可解决；是否计入主指标需看事前协议 |
| TIMEOUT | PASS | Typer Pilot R1；保留 timeout 与功能通过两维 |
| Provider 429 / task infra failure | 未执行 | 记录为故障，不混入可评分模型失败；仍计入总 Trial/可靠性分母 |

## 5. 先做的离线测试（本阶段尚未执行）

| ID | 反例 | 预期 |
|---|---|---|
| E09-T01 | Provider Error after trial start | 分类为 Provider 故障，未凭空获得 scoreable |
| E09-T02 | runner_factory before try 抛异常 | 明确 suite 是否继续且给出失败记录 |
| E09-T03 | materialize_task_repo 失败 | 不算模型调用或模型失败 |
| E09-T04 | INCOMPLETE 但独立 grader PASS | 保存两个结果轴 |
| E09-T05 | FINISH 但 grader FAIL | 不报告任务成功 |
| E09-T06 | 外部 Timeout 后 verifier PASS | 结果一致地保留双方 |
| E09-T07 | runner.close 失败 | 单独记录清理问题，不覆盖模型终态 |
| E09-T08 | Fake Backend 成功 | 不能输出真实模型能力成功率 |
| E09-T09 | Eval factory 与 CLI config 不匹配 | 必须显示差异，不允许不公平对照 |
| E09-T10 | Trace 缺失/截断 | 不发出没有证据的过程评分 |

**不应现在直接改动历史成功率或 H0/H1 生产代码**。按 E09-T01..10 设计离线异常注入测试，再定义报告 schema 兼容策略和最小功能改动。Stage 09 余下 H01–H10 各主题及 Pi/Codex 等项目的全量 Trace/Eval 横向对照仍需单独完成。

## 6. 2026-11-14 新增现状刻画测试（待真实仓库执行）

在 `tests/test_coding_agent_eval.py` 追加了两个测试：

1. `test_characterization_runner_exception_is_counted_in_real_model_rate`：以模拟 Provider/Runner 异常触发 `EvaluationHarness._run_trial` 的 except 分支，检查当前 `execution_status=executed`、`real_model_executed=True` 和 `observed_success_rate=0.0` 的行为。这是证明**当前统计协议的风险**，并不主张这是未来应该保留的正确分母。
2. `test_characterization_runner_factory_exception_aborts_suite`：模拟工厂在 `runner.run` 前抛异常，检查当前 suite 异常中断、未形成 `trial.json` 的行为。

上述两项已做独立 Python 代码片段语法检查，**尚未在完整 Forge 项目中运行 pytest**（当前容器无法 DNS 解析 GitHub 进行 clone）。它们是后续修正 H0 前的 characterization tests，不代表已经改进分类器或成功率报告。

无付费 API 的本地验证命令：

~~~bash
python -m pytest -q tests/test_coding_agent_eval.py tests/test_completion_candidate_probe.py
~~~

## 7. 2026-11-14：H0 独立评分合同原型验证增量

详见 [09-离线评分合同验证记录.md](09-离线评分合同验证记录.md)。本报告上述“尚未运行”的描述属于**源码初审时点**，不是当前进度。

- 新增 `evals/trial_outcome_probe.py`，以 `TrialEvidence` + 预冻结 `ScoringPolicy` 评估 `scoreable`，保留原始 verifier、自然 FINISH、rollout 和故障域等分离字段；策略内容 SHA-256 绑定每条 Trial 决策，避免同名策略下变更分母条件造成结果混用。
- 新增 `tests/test_trial_outcome_probe.py`，含 21 条离线测试。本地重新执行 Stage08 的 19 条测试，合计 **40 passed in 0.08s**（Python 3.13.5、pytest 9.0.2）。这是**研究原型回归**，不是原有 `EvaluationHarness` 行为通过，也不是 Benchmark 的 success rate。
- `tests/test_coding_agent_eval.py` 的 2 个真实 Forge Evaluation 行为刻画用例**依然未运行**；当前容器无法通过 DNS 拉取完整 Github repo，因此留给有完整 checkout 的本地环境进行无 API 的 pytest。
- **H0 未完成生产接入**：原型中的 `model_call_started`、`failure_domain`、`snapshot_within_deadline` 均需要可信来源，现有 `TrialResult` 不能直接提供，不能从异常消息或配置标志推断。严禁把独立原型的分母公式替换到生产报表后直接报告模型能力。
- Stage09 仍为 H0/H1 源码关键路径初审，不是 H01–H10 全部完成；H1 配置漂移与 Stage03/07 执行凭据隔离保持待办。

## 8. 2026-11-14：H0 可信数据源核查与真实 Typer 归档

详见 [09-H0-Trace与Harbor证据来源核查.md](09-H0-Trace与Harbor证据来源核查.md)。

- 新增只读 `examples/harbor/trace_evidence.py`，依据 `model_call_id/run_id/step_id` 检验 Trace 事件关联，区分正常 finished 调用、pending request、自然 `run_terminated` 与 Harbor 外层 `AgentTimeoutError`，并保留原始 Verifier reward、缺失 Usage、未知费用。
- 使用 GitHub 连接直接解析归档的全部 324 个 Trace 事件，得到开始 30、完成 29、Step30 未完成、两次 429 retry、没有正常 `run_terminated`；29 次 completed usage=868,723 Tokens，与归档 Manifest 的计数一致。
- 归档 Harbor 的 `agent_result` 用量全部 null，但外部 verifier reward=1.0。源码发现 `examples/harbor/forge_harbor_agent.py` 是在 `await environment.exec()` 返回后才汇总并回写 usage；外层超时可能绕开这一步，因此旧 Harbor README 对“缺少终态时不会产生 reward”的概括已修正。
- `tests/test_trace_evidence.py` 的 **11 条合成用例本地通过**；其余两个读取真实归档的 Python 回归已提交但**尚未在完整 Forge checkout 中执行**。本地独立 Stage08/09 原型累计 **51 passed**。这些测试不代表模型任务成功率。
- `trace_evidence.py` 故意输出 `scoreable=null`、`failure_domain=null`、`provider_billed_cost=null`：外层超时证明 watchdog 超时，不能直接归罪模型或把 Provider 用量估算当真实账单。Stage09 的 H0/H1 生产接线仍未开始。
