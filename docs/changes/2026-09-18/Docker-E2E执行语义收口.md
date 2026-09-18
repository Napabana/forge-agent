# Docker E2E 执行语义收口

日期：2026-09-18  
基线：`dev@084426470d7e79da0d1d6db8ec6d31cb70c55bb8`

## 设计结论

- P0-A Completion Guard：成立，复用现有 repository fingerprint，不新增状态系统。
- P0-B Single Permission：成立，ToolExecutor + PermissionManager 作为生产唯一权威。
- P1-C Sandbox path semantics：成立，host workspace 与 model-visible execution workspace 分离，不修改内部 Task.repo_path。

## 实现

- Completion Guard 改为 repository-state-driven：Tool 后统一比较 `repository_fingerprint`，记录 `last_repo_change_step`，同一状态同时供 loop detector 与 Repo Map 刷新使用。FINISH 用 final vs initial state 判断 required changes，并要求成功测试不早于最后真实 repo change。
- 生产 confirmation 只由 ToolExecutor + PermissionManager 决策；`_build_registry` 不再把同一 callback 注入 ShellTool。standalone ShellTool API 保留。
- 新增 `AgentConfig.execution_workspace`。Sandbox 下模型只看到 `/workspace` 与相对路径指引；host Task.repo_path/worktree 仍用于 file tools、PermissionManager、Repo Map、repository fingerprint 和 worktree lifecycle。DockerRuntime cwd 映射未修改，也未 rewrite shell command。
- `PytestTool` 已明确名为 `test` 且 description 为 Run pytest，本轮不增加 `run_tests` alias。

## Regression

覆盖 shell 真修改、test 后 shell 再修改、无真实变化、恢复 initial state、file_write、HEAD commit、生产 confirm yes/no/callback crash、sandbox prompt 与 host workspace 分离。既有 `tests/test_sandbox.py` 继续固定 Docker cwd translation/LocalRuntime，`tests/test_confirm.py` 继续固定 standalone ShellTool confirmation。

## 验证状态

2026-09-18 用户已在本地完成真实 Docker E2E 验证，本轮执行语义收口按实际产品入口验收通过。

真实执行命令：

```bash
agent run \
  --repo "$TARGET_REPO" \
  --task "在 calculator.py 中新增 cube(a: int) -> int 函数，并在 tests/test_calculator.py 中添加正数、负数和零的测试，运行完整 pytest 验证。不要提交 git commit。" \
  --isolate \
  --sandbox \
  --result-policy discard \
  --confirm
```

观测结果：

- Agent 在独立 worktree 中完成读取、修改、验证与 FINISH；
- 两次具有写副作用的 shell 命令分别只触发一次 Permission confirmation，没有重复确认；
- shell 写入后 Completion Guard 能识别真实 repository state 变化；
- 先用 shell 执行 `python -m pytest -q` 得到 18 passed；
- 首次 FINISH 因未使用 dedicated `test` tool 被 Completion Guard 拒绝，Agent 随后自动恢复并调用 `test`；
- dedicated `test` 再次得到 18 passed，随后正常完成；
- 最终 `Status: SUCCESS`，`Steps: 9`，记录的 Token 数为 52,258，运行时间为 157.0s；
- `result-policy=discard` 正常移除 worktree；用户确认本轮完整流程验收通过。

上述数字只记录本次真实 E2E case，不外推为总体成功率、平均成本或性能指标。

本地回归建议：

```bash
pytest -q \
  tests/test_agent_completion_guards.py \
  tests/test_tool_lifecycle_p0_2.py \
  tests/test_execution_workspace.py \
  tests/test_orchestrate.py \
  tests/test_sandbox.py \
  tests/test_confirm.py

pytest -q \
  tests/test_runner.py \
  tests/test_chat.py \
  tests/test_api.py \
  tests/test_cli_isolate.py \
  tests/test_github_issue_delivery.py \
  tests/test_trace_v2.py

pytest -q
```

真实 Docker E2E：

```bash
cd /mnt/e/2806/forgeAgent/forge-agent

export TARGET_REPO=/mnt/e/2806/forgeAgent/pr-test-docker-e2e

agent run \
  --repo "$TARGET_REPO" \
  --task "在 calculator.py 中新增 cube(a: int) -> int 函数，并在 tests/test_calculator.py 中添加正数、负数和零的测试，运行完整 pytest 验证。不要提交 git commit。" \
  --isolate \
  --sandbox \
  --result-policy discard \
  --confirm
```

验收关注：shell 真修改不再错误触发 `REQUIRED_CHANGE_MISSING`；测试后真实变更必须重测；危险 shell 只确认一次；sandbox 模型使用相对路径或 `/workspace`；pytest 通过；discard 清理 worktree；原 target repo 不被业务修改污染。step/token/time 只观察趋势，不写成硬指标。

## 未改边界

- 不重写 Agent loop；
- 不增加第二套 repository state 或 permission system；
- 不 rewrite shell command；
- 不把 `/workspace` 写回 host Task.repo_path；
- 不改变 Independent AcceptanceContract；
- 不修改 `config/default.yaml`、B1/B2 fixture 或 `evals/results`。
