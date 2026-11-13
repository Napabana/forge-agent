# Forge Agent 运行期问题修复规划与实施记录

> 日期：2026-11-13  
> 实施基线：GitHub `dev` 初始 HEAD `46aa2b4d4f10a871013936636814603186b4dcbf`  
> 目标：修复真实 Qwen3.5-4B 仓库任务暴露的 Context 配置/接线缺陷，以及 Runtime 命令参数边界问题。  
> 状态：运行期修复已推送。维护者首次全量 pytest 为 1117 passed、14 skipped、1 failed；失败为既有指数退避测试的全局 `time.sleep` patch 不稳定。已改为注入 `cancel_event.wait` 记录等待，待再次执行专项与全量 pytest。Docker E2E 和真实模型回归仍待执行。

## 1. 背景与审查结论

本轮问题来自 Qwen3.5-4B 在真实仓库任务中的运行记录，以及随后对当前 `dev` 源码的审查。修复目标不是新增一套 Agent 架构，而是让已有的 Repo Map、TokenBudget、TraceableCompaction、Permission 和 Runtime 能力在生产入口中按同一配置生效。

本轮审查对原始报告做了以下事实校正：

1. **Repo Map 已经计入 Context pressure。** `agent/core.py::_render_request_parts` 把 Repo Map 嵌入 system prompt；`TokenBudget.request_pressure` 统计完整 system text，因此 Repo Map 已经计入 `system_tokens`。独立的 `repo_map_tokens` 只用于诊断，不能再次累加。
2. **`RunRequest.prepare_next_turn` 本身有效。** `ExecutionRunner.run` 会把 request callback 合并进 AgentConfig。真正缺失的是生产入口没有统一创建和注入 Compaction Policy。
3. **`history_window` 当前不执行裁剪。** `ConversationHistory.add` 始终追加 canonical history；`max_messages` 只是兼容提示字段，不应描述为有效消息数量上限。
4. **默认 Docker 镜像包含 Python 3.11 与 pytest。** `tools/sandbox.Dockerfile` 使用 `python:3.11-slim` 并安装 pytest。解释器问题应通过 Runtime 能力和自定义镜像配置处理，不能假设默认镜像缺少 `python3`。
5. **Harbor 报告项不在本轮远端基线内。** 当前 `dev` 无法读取 `forge_harbor_agent.py`，因此不创建猜测实现；待该文件实际进入分支后单独审查 `_extra_env` 生命周期。

## 2. 最终设计决策

### 2.1 Repo Map

- 在 `ContextConfig` 中公开 `repo_map_mode` 与 `repo_map_budget`。
- 支持 `none / static / query_aware / incremental`，默认 `incremental`。
- `repo_map_budget` 是硬上限，不是目标值：

```text
effective_repo_map_budget = min(available_input_tokens * 0.15, repo_map_budget)
```

- 保留原 15% 动态分配，避免配置 cap 大于当前预算时挤占其他分区。
- 通过显式 `TokenBudget(repo_map_cap=...)` 传递，不把 Repo Map 策略塞入 `ContextBudgetSpec`。
- Runner Agent cache key 纳入 `repo_map_budget`，避免配置变化后复用错误 Agent。
- 生产配置 `ContextConfig.repo_map_budget` 默认硬上限为 12000 tokens；`AgentConfig` 的程序化默认值保持 `None`，避免改变既有独立调用和冻结的 benchmark 行为。

### 2.2 Context Budget 与 Compaction

- 新增 `entry/context_common.py::build_context_policy`，为每个 run/session 创建独立策略实例。
- `compaction_mode` 支持 `off / traceable`，默认 `traceable`。
- 默认参数：threshold=0.70、target_ratio=0.50、keep_recent_tokens=8000。
- CLI run、CLI chat、API、GitHub Issue 均从同一配置工厂创建策略；Chat 不再独占 Compaction。
- Fresh task 的首个模型请求只有任务输入，不触发历史压缩；`Agent.run` 后续 step 在下一次模型调用前执行 policy。已有 Chat shared history 仍由 Runner 的 shared-history boundary 处理首轮 preflight。
- Policy 实例不跨独立任务共享，避免 checkpoint lineage、active view 和 side-call usage 污染。
- Runner 在正常返回时统一消费 semantic compaction side-call usage 并合并进 `RunResult.usage` / `total_tokens`；Chat 的中断路径仍由 `ChatSession` 负责消费尚未归集的 usage。

### 2.3 小模型默认预算

当前默认配置使用 Qwen3.5-4B，但 SiliconFlow 实际服务端上下文窗口没有在仓库中得到核实。因此：

- 不填写未经核实的 `llm.context_window`。
- `agent.context_budget_cap=131072` 作为 Forge 自身预算上限与未知能力 fallback。
- `agent.context_safety_margin_tokens=8192`。
- `llm.max_output_tokens=8192`。
- 该配置不声称模型或代理服务端原生支持 131072/262144；实际窗口必须以服务端模型页面和返回错误/usage 为准。
- 按当前 fallback 语义，有效输入预算为 114688 tokens（131072 - 8192 output reserve - 8192 safety margin）。

### 2.4 Trace Token Breakdown

- `Agent._trace_token_breakdown` 接收当前 `TokenBudget`。
- system、Repo Map、planning、tool schema、history、injected message 全部复用 `TokenBudget.counter`。
- Trace 增加 `counter_name`、`counter_estimated`、`capability_fallback`，明确本地统计是请求前估算，不冒充 Provider billing usage。
- Repo Map 在完整 system prompt 中只计入一次。

### 2.5 Runtime argv 与 Pytest/Git

- Runtime 新增 `exec_args(argv)`；LocalRuntime 和 DockerRuntime 使用参数数组直接执行，不经 Shell 二次解析。
- Runtime 提供 `python_executable`：LocalRuntime 使用当前 `sys.executable`，DockerRuntime 默认使用 `python3`。
- PytestTool 使用 argv 执行；`args` 使用 `shlex.split` 解析，path 保持为独立参数并拒绝 option-like path。
- 增加 `tools.test.python_executable`，允许自定义镜像指定解释器路径；未配置时使用 Runtime 默认值。
- GitTool 改用 argv 执行；`git add` 在文件路径前添加 `--`。
- ShellTool 仍是有意提供给 Agent 的 shell command 能力，继续由 PermissionManager 管理；本次不将显式 Shell 命令改造成 argv。

### 2.6 Permission 与 History

- CONFIRM 无 callback 时返回模型可操作的拒绝原因：没有交互审批通道、操作未执行、不要重复尝试，继续使用当前策略允许的操作。
- 用户明确拒绝仍保留 `Permission denied by user` 语义。
- `history_window` 暂时保留兼容，但标记 deprecated；不新增按消息数量破坏性裁剪。
- Harbor `_extra_env` 项因文件不在当前 `dev` 而延期，不虚构修复。

## 3. 实施范围

| 文件 | 变更 |
|---|---|
| `config/schema.py` | ContextConfig、TestToolConfig、模式与预算校验 |
| `config/default.yaml` | Repo Map、Compaction、Context cap、安全余量与 Test interpreter 默认值 |
| `context/token_budget.py` | Repo Map hard cap |
| `context/compaction.py` | 默认阈值统一为 0.70 |
| `agent/core.py` | Repo Map cap 透传；Trace 复用同一 Counter |
| `agent/runner.py` | cache key 纳入 Repo Map budget；统一合并 Compaction side-call usage |
| `entry/context_common.py` | 生产入口共享 Policy 工厂 |
| `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py` | 四入口配置透传和 Policy 接线 |
| `tools/runtime.py` | Local/Docker argv 执行、Python interpreter 能力 |
| `tools/test_tool.py`、`tools/git_tool.py` | Pytest/Git 命令改为 argv |
| `harness/executor.py` | CONFIRM 无交互通道时的明确拒绝 |
| `tests/test_runtime_context_fixes.py` | 新增配置、预算、argv、权限、Trace 和 Usage 回归测试 |
| `docs/design/02-Agent与Tool生命周期.md` | 同步 Runtime/Permission 事实 |
| `docs/design/03-Context与RepoMap.md` | 同步配置、预算、Compaction 入口和 History 语义 |
| `USAGE.md`、`docs/README.md` | 更新配置示例、验证说明与文档导航 |

## 4. 验收标准

### 4.1 配置与 Context

- [ ] 默认 Repo Map mode 为 incremental，配置可切换四种 mode。
- [ ] Repo Map 实际 build budget 不超过 `min(15% available, repo_map_budget)`。
- [ ] CLI run/chat、API、GitHub Issue 使用相同 Compaction 配置。
- [ ] compaction_mode=off 时不创建 Policy；traceable 时每次 run/session 使用独立实例。
- [ ] 压缩只改变 model-facing view，不覆盖 canonical History。
- [ ] Semantic side-call usage 计入 RunResult；Chat 中断路径不丢失 pending usage。
- [ ] Trace Token Breakdown 使用同一 TokenCounter，Repo Map 不重复计数。

### 4.2 Runtime 与权限

- [ ] LocalRuntime argv 执行不启用 shell。
- [ ] DockerRuntime 通过 `docker exec ... COMMAND ARG...` 传递 argv，不经过 `bash -c`。
- [ ] Pytest 默认解释器分别为本地当前 Python 与 Docker `python3`。
- [ ] 自定义 Test interpreter 配置可覆盖 Runtime 默认值。
- [ ] 含空格、分号、Shell 元字符的文件路径仍作为单个 argv 参数。
- [ ] GitTool 的路径参数不会被 Shell 重新解释，`git add` 使用 `--`。
- [ ] 无交互审批通道时 CONFIRM 操作不执行，并向模型说明原因。

### 4.3 回归命令

先运行新增专项：

```bash
python -m pytest -q tests/test_runtime_context_fixes.py
```

再运行已有相关测试：

```bash
python -m pytest -q \
  tests/test_compaction.py \
  tests/test_model_aware_token_budget.py \
  tests/test_tool_pruning.py \
  tests/test_structured_compaction.py \
  tests/test_prepare_next_turn.py \
  tests/test_tool_lifecycle_p0_2.py \
  tests/test_execution_constraints.py \
  tests/test_sandbox.py \
  tests/test_trace_v2.py
```

最后执行：

```bash
python -m pytest -q
```

Docker 验收需另外确认本机 Docker daemon 可用，并执行 isolate+sandbox 测试。真实模型回归不属于本轮自动执行范围，避免在没有用户明确安排时调用 Provider。

## 5. 本轮执行状态

- [x] 修复规划和决策写入本文档。
- [x] Repo Map 配置与预算 cap 实现。
- [x] Compaction Policy 工厂接入四个生产入口。
- [x] Semantic Compaction usage 在 Runner 正常返回路径归集。
- [x] Trace Token Breakdown 复用 TokenBudget Counter。
- [x] Runtime argv、Pytest interpreter、GitTool argv 实现。
- [x] CONFIRM 无交互通道提示修复。
- [x] 新增专项回归测试文件。
- [x] 扩充 Repo Map 模式校验、Runtime 子类兼容性以及 Trace 新字段的回归覆盖。
- [x] 程序化 AgentConfig 保持旧 budget 语义；在生产入口显式施加 Repo Map cap。
- [x] 更新使用手册和架构设计文档。
- [ ] 本地 pytest 专项/全量测试：待维护者拉取后执行。
- [ ] Docker E2E：待维护者本地 Docker 环境执行。
- [ ] Qwen3.5-4B 真实仓库回归：待维护者安排；本轮没有调用 Provider。

本轮不能将“文件已写入 GitHub”描述为“测试全部通过”。最终验证结果应由拉取后的实际测试输出补充。


## 6. 首次本地全量回归后的跟进

维护者首次拉取后执行全量测试，结果：

- 1117 passed
- 14 skipped
- 1 failed
- 30 warnings
- 总耗时约 7 分 54 秒

唯一失败：`tests/test_day7.py::TestCallWithRetry::test_exponential_backoff`。失败断言为第二次等待时间不等于第一次的两倍；实际捕获值分别为约 `1.00039` 和 `2.13495` 秒。

源码核对结论：

1. `Agent._call_with_retry` 的重试实现与本轮修复前的基线提交 `46aa2b4d` 完全一致；本轮没有修改指数退避算法。
2. 首次修复提交集合没有修改 `tests/test_day7.py`。
3. 旧测试通过全局 `patch("time.sleep")` 记录等待。全局替换可能捕获同一进程其他线程的 sleep，不能可靠隔离当前 Agent 的退避等待。
4. 重试实现已经支持通过 `cancel_event.wait(delay)` 执行可取消等待，因此测试可以注入一个记录型 Event，而无需替换全局函数。

跟进修复：

- [x] 将测试改为注入 `RecordingCancelEvent`，仅记录当前 Agent 的 `wait()` 调用。
- [x] 显式设置 `llm_retry_jitter=0.0`，消除随机抖动对断言的影响。
- [x] 精确断言等待序列为 `[1.0, 2.0]`，并断言 Backend 调用三次。
- [ ] 单测、Day 7 测试文件及全量 pytest 重新执行，结果待维护者确认。

复测命令：

```bash
python -m pytest -q tests/test_day7.py::TestCallWithRetry::test_exponential_backoff
python -m pytest -q tests/test_day7.py
python -m pytest -q
```

AnyIO 的 `Task.cancel(msg)` / `Future.cancel(msg)` 弃用警告来自当前测试环境依赖栈，不是这次唯一失败的原因；本轮先记录，不扩大修改范围。
