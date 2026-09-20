# Direct Run Git Delivery Ownership 收口

日期：2026-09-20  
实现提交：`f73fe5cef064e0b6a18bd958d90679f050d643ef`  
状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

## 问题

真实 Stage 1 运行中，同一个普通 `agent run` 任务曾出现两种行为：

```text
run A: 修复 + 测试 + git add + git commit + FINISH
run B: 修复 + 测试 + FINISH
```

是否 commit 取决于模型当轮决策，导致：

- 用户无法从入口语义预先知道是否会产生提交；
- `git_add/git_commit` schema 每轮占用模型上下文；
- 模型可能在任务已完成后继续进入 Git cleanup / permission / recovery 路径；
- Delivery responsibility 与 coding loop responsibility 混淆。

## 收口

`entry/cli.py` 新增 `allow_git_mutation` registry capability，并增加：

```python
_build_run_registry(...)
```

普通 `agent run` 和 `agent run --isolate` 固定使用该 registry：

```text
Agent-visible Git tools:
- git_status
- git_diff

Not exposed:
- git_add
- git_commit
```

因此 direct run 的 commit 行为不再由模型自由决定。

`_build_registry()` 默认仍保留原有 Git mutation tools，避免破坏其它既有入口；GitHub Issue→PR 路径已有独立 delivery contract，在 create_pr 模式下同样移除 Agent 的 `git_add/git_commit`，由 `deliver_pull_request()` 在独立验收后确定性执行 add / commit / push / PR。

## Prompt 收口

删除 system prompt 中用于反复提醒模型“不要 commit / 优先 Git tools”的两条规则。

`ShellTool.description` 只保留最小边界：

```text
Do not use shell for mutating Git operations.
```

真正的 commit ownership 由 capability/runtime 决定，而不是依赖 prompt compliance。

## 测试

新增 `test_run_registry_hides_git_mutation_tools`，验证普通 run registry：

- 保留 `git_status`
- 保留 `git_diff`
- 不包含 `git_add`
- 不包含 `git_commit`

同时更新 system prompt 测试，确保不再把 commit policy 委托给模型提示词。

本日志创建时尚未收到用户本地 pytest 结果，因此不补造通过数量或耗时。
