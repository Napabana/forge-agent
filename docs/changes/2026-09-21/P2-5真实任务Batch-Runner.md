# P2-5 前置：无人值守真实任务 Batch Runner

日期：2026-09-21

状态：**IMPLEMENTED / REAL PROVIDER PARTIAL VERIFIED / STEP-BUDGET TUNED**

## 目标

为后续 P2-5 Trajectory-driven Skill Evolution 生成更有价值的真实 accepted trajectories，新增一个薄 orchestration driver：

`scripts/run_pr_test_practice_batch.py`

本轮只实现 Batch Runner、独立 acceptance、resume、dry-run 与 deterministic 验证；**没有执行任何 DeepSeek / OpenAI / Anthropic / KRILL Provider 请求**。

目标测试仓库固定为用户准备的独立 `pr-test` 工作树，基线分支默认：

`forge-p2-mcp-demo`

Task A/B/C 在同一个 working tree 中串行执行，后一个任务建立在前一个任务的修改结果上。

## 源码审计依据

本轮以当时远端 `dev@06f97407c56b2fab0ff22369cd161665996ac625` 为实现前基线，重点确认：

- `agent/runner.py`
  - `AcceptanceContract`
  - `RunRequest`
  - `ExecutionRunner.run`
  - `_apply_independent_acceptance`
  - `_record_post_run_trace_log`
- `agent/task.py`
  - `Task`
  - `RunResult`
  - `RunStatus`
  - `infer_completion_requirements`
- `entry/event_renderer.py`
  - `RunEventRenderer`
- `agent/event_log.py`
  - `EventLog`
  - Trace v2 acceptance / run termination 写盘
- `entry/cli.py`
  - `_build_run_registry`
  - CLI 的 `AgentConfig` 组合方式
- `config/default.yaml`
  - 当前真实连接：`openai / deepseek-v4.1-flash / KRILL_API_KEY`
  - MCP 默认关闭
- `config/schema.py`
  - `AppConfig` / `AgentCfg` / `MCPConfig`
- `experience/trajectory.py`
  - `load_trajectory`
  - positive mining 条件：
    - `run_status == "success"`
    - `acceptance_status == "passed"`

实现没有引用不存在的 `config/eval-p2-planning-recovery-v2.yaml`。

## 为什么直接复用 ExecutionRunner

普通 CLI run 在没有 independent verifier 时，最终可能是：

`acceptance_status=not_requested`

而 P2-5 当前只接受：

`run_status=success AND acceptance_status=passed`

因此 Batch Runner 不通过事后修改 Trace 补字段，而是每个任务直接构造：

`RunRequest(task=..., acceptance=AcceptanceContract(..., verifier=...))`

Runner 在 Agent 成功后执行 runner-owned verifier，随后按现有 Trace v2 语义追加：

- `acceptance`
- `run_terminated`

Batch Runner 还会在 run 结束后调用 `experience.trajectory.load_trajectory(trace_path)` 再检查一次 `normalized.eligible`。因此脚本自身的“P2-5 eligible”判断与 P2-5 当前 positive mining 语义保持一致。

## Batch Runner 架构

执行链：

```text
one command
  ↓
repo / branch / state preflight
  ↓
Task A
  ↓
ExecutionRunner
  ↓
independent AcceptanceContract.verifier
  ↓
Trace v2 eligibility check
  ↓
SUCCESS + acceptance=passed + P2-5 eligible ?
  ├─ no  -> STOP
  └─ yes
        ↓
      Task B
        ↓
      same checks
        ↓
      Task C
        ↓
      batch summary
```

A/B/C 共用同一个目标 working tree，不创建三个 isolate worktree。

脚本没有修改：

- `Agent.run`
- `ExecutionRunner` 核心 contract
- Trace schema
- Acceptance semantics
- P2-5 implementation
- SkillRuntime
- EvaluationHarness
- `config/default.yaml`

## Dry-run / execute 边界

默认不执行 Provider。

`--dry-run` 或不提供 `--execute` 时：

- 检查 repo / .git / branch / clean state；
- 读取当前配置并打印 provider/model；
- 打印 Task A/B/C；
- 打印 verifier 摘要；
- 打印 artifact 路径；
- 打印预计执行顺序；
- **不会调用 `_build_execution_runner`；**
- **不会实例化 LLM backend；**
- **API calls = 0。**

只有显式 `--execute` 后，才会进入 `_build_execution_runner` 并调用 `create_backend_from_config`。

## Runtime overrides

仅在 Batch Runner 的内存配置中覆盖：

- `max_steps = 60`（Batch Runner 默认值，可通过 `--max-steps` 覆盖）
- `planning_mode = "always"`
- `recovery_mode = "structured"`
- `skills_enabled = False`
- `MCPConfig(enabled=False)`

不修改 `config/default.yaml`。

普通 run registry 复用 `entry/cli.py::_build_run_registry`，因此模型没有 git add/commit 工具。

Batch 使用 `PermissionManager(workspace=repo)` 且没有交互 confirm callback：无人值守过程中不要求用户中途输入；文件/测试/专用 Git 只读工具可以正常执行，无法证明只读的 shell mutation 不会被自动确认。

## Task A：Calculator Core Architecture

固定目标：

```text
src/calc_core/
├── __init__.py
├── errors.py
├── operations.py
├── registry.py
└── service.py
```

保留 `calculator.py` 兼容 facade。

冻结 contract 包括：

- add / subtract / multiply / divide / clamp 兼容；
- divide-by-zero 原 contract；
- clamp 原 boundary / invalid-bounds contract；
- `CalculatorError` / `UnknownOperationError`；
- `OperationRegistry.register/resolve`；
- `CalculatorService.execute`；
- unknown operation 必须是 domain error；
- focused tests + full regression；
- 不创建 commit。

独立 verifier：

1. 直接导入旧 `calculator.py` API；
2. 验证五个函数与异常消息；
3. 独立构造 `OperationRegistry`；
4. 验证 `CalculatorService` 默认五操作；
5. 验证 unknown operation 抛 `UnknownOperationError`；
6. 最后运行完整 `python -m pytest -q`。

## Task B：Batch Execution

新增 `CalculatorService.execute_batch(requests)`。

冻结结构化结果：

- success：`{"ok": True, "value": ...}`
- failure：`{"ok": False, "error": {"type": ..., "message": ...}}`

独立 verifier 先完整重跑 Task A probe，再固定验证：

```text
success(add)
failure(divide-by-zero)
success(multiply)
```

确认：

- 结果数量为 3；
- 顺序不变；
- 中间错误不终止 batch；
- 前后成功值正确；
- 中间错误类型可确定识别；
- unknown operation 独立捕获；
- invalid request 为 `InvalidRequestError`；
- 最后运行完整 pytest。

## Task C：Runtime Operation Policy

新增 `OperationPolicy`，固定：

- `enabled_operations`
- `max_batch_size`
- `strict_validation`

独立 verifier 在完整重跑 Task A/B probe 后继续验证：

- default policy compatibility；
- disabled single operation；
- disabled batch item；
- `len == max_batch_size` 允许；
- `len > max_batch_size` deterministic reject；
- strict=false extra key 保持兼容；
- strict=true extra key 返回 `InvalidRequestError`；
- 最后运行完整 pytest。

## Resume 语义

`batch_state.json` 保存：

- repo absolute path；
- base branch；
- base HEAD；
- working-tree snapshot；
- 每个 Task 的 status / acceptance_status；
- trace_path；
- steps / tokens / elapsed；
- trajectory eligibility。

snapshot 包含 tracked diff、staged diff、status 与 untracked file contents。

`--resume` 时：

1. 校验 repo path / branch / HEAD 没有变化；
2. 校验 working tree 与上次 checkpoint fingerprint 一致；
3. 只把同时满足：
   - `status == success`
   - `acceptance_status == passed`
   - `trajectory_eligible == True`
   的任务视为可跳过；
4. 已通过任务不会重新调用 API；
5. 失败任务从当前已保存的 working tree checkpoint 继续；
6. 未知的人工修改会在 Provider 构造前拒绝 resume。

当 A/B/C 都已经通过时，`--execute --resume` 会在构造 backend 前直接退出，避免重复消耗额度。

另外，若 Provider/框架以异常形式中止而不是返回普通 `RunResult`，Batch Runner 会先记录当前 working-tree fingerprint 和失败状态再停止；不会自动改 prompt 或自动重试。这样下一次 `--resume` 仍能从已记录 checkpoint 判断是否可以安全继续。

`--only A|B|C` 也已支持；若直接执行 B/C，其前置任务需要通过离线 verifier。

## Artifact

默认输出：

`evals/results/pr-test-practice-batch-<UTC timestamp>/`

包含：

- `batch_state.json`
- `batch_summary.json`
- `traces/*.jsonl`

summary 可直接回链每个成功 Task 的原始 Trace v2，并列出 `eligible_traces_for_p2_5`。

## Deterministic validation

本窗口没有执行真实 Provider。

已完成：

```text
python -m py_compile run_pr_test_practice_batch.py test_pr_test_practice_batch.py
PASS
```

专项测试在 no-provider stub config 环境：

```text
5 passed in 0.12s
```

覆盖：

- resume 只跳过真正 accepted + eligible 的任务；
- A/B/C hidden probe 都能编译；
- dry-run 不进入 provider runner；
- 全部已通过的 execute+resume 不进入 provider runner；
- working tree 被人工改动后 resume fail-closed。

另使用 synthetic calculator implementation 实际执行三个 hidden verifier：

```text
A True
B True
C True
```

每个 verifier 均真实执行 probe + synthetic repo 的完整 pytest。

实际 dry-run（no-provider harness）输出确认：

```text
Forge Agent pr-test practice batch — DRY RUN (API calls = 0)
Provider   : openai
Model      : deepseek-v4.1-flash
Overrides  : planning=always, recovery=structured, skills=false, mcp=disabled, max_steps=60
...
Order      : A -> B -> C
```

限制：

当前执行容器直接 clone GitHub 时 DNS 解析失败，因此没有在容器中对完整 `forge-agent` checkout 运行仓库级 pytest，也没有对真实 `pr-test-agent-practice` 执行 Task A/B/C。不能把上述 deterministic validation 表述成真实模型任务成功。

## 真实 Provider 运行反馈（2026-09-21）

用户随后在本地对同一 batch artifact 执行了真实 `deepseek-v4.1-flash / KRILL` 运行，得到以下直接证据：

- Task A 首轮在 `max_steps=30` 时达到 step limit，状态 `incomplete`，未进入 acceptance。
- 保留 checkpoint 后执行 `--resume`，Task A 在 21 steps 内补齐缺失的 `src/calc_core/__init__.py`，focused tests 12 passed，full regression 28 passed，最终：
  - `status=success`
  - `acceptance=passed`
  - `P2-5 eligible=success_with_acceptance_pass`
- Task B 在同一 working tree 上继续执行，主体实现和测试已写入；到 step 22 后进入 collection-error 诊断/修复，最终再次在 step 30 命中 `max_steps`，状态 `incomplete`。
- 这两次真实运行说明 `30` 对 `planning=always + recovery=structured` 的中等复杂度 coding task 过紧：A 实际跨两次运行累计需要约 51 个 Agent steps；B 在 30 步时仍处于有效修复阶段而非无进展循环。

因此 Batch Runner 将默认 step budget 从 30 调整为 60，并新增 CLI 参数：

```text
--max-steps N
```

该值统一传入：

```text
CLI
  -> config.agent.max_steps
  -> AgentConfig.max_steps
  -> Task.max_steps
```

另外，真实运行暴露出前台可观测性问题：失败的 test observation 同时具有 pytest output 与 error，但旧 `RunEventRenderer` 只显示 `error or output`，导致终端只看到 `pytest exited with code 2`。现已改为失败时先展示 output preview，再单独展示 error，便于无人值守运行时直接看到 collection traceback。

## 下一步

用户本地 pull 最新 `dev` 后：

1. 准备独立、clean 的 `pr-test-agent-practice`，分支为 `forge-p2-mcp-demo`；
2. 可先执行 Batch Runner dry-run，确认 repo/config/order；
3. 用户确认后再显式执行唯一的 `--execute` 命令；
4. 真实 A/B/C 完成后，从 batch summary 提取 eligible Trace v2，再进入 P2-5 candidate generation/evaluation。

本轮到此停止，不替用户触发真实 Provider。


## 最终真实 Batch 结果（2026-09-21）

将 Batch Runner 默认 step budget 调整为 60 后，用户从同一 checkpoint 继续执行：

- Task A：已是 `SUCCESS / acceptance=passed / P2-5 eligible`，resume 正确 SKIP，未重复调用 API。
- Task B：31 steps / 381,712 tokens / 692.2s，focused batch tests 19 passed，full suite 47 passed，最终 `success + acceptance=passed + P2-5 eligible`。
- Task C：32 steps / 602,226 tokens / 488.4s，focused policy tests 21 passed，full suite 68 passed，最终 `success + acceptance=passed + P2-5 eligible`。
- 本次成功 batch summary：84 steps / 1,227,602 tokens / 1439.1s。
- eligible traces 共 3 条，A/B/C 全部可作为 P2-5 positive mining 输入。

B 运行中出现一次 LLM timeout 和一次 connection error，均由现有 bounded retry 恢复，没有导致最终任务失败。

因此本 Batch Runner 的真实任务生成目标已经完成。后续不再继续生成额外 task，而转入 `scripts/run_skill_evolution.py --mine-only` 做真实 Trace mining/candidate generation。
