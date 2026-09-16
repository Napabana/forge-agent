# pr-test 真实 PR 改动内容

## 本轮目标

使用用户指定的 `Napabana/pr-test` 完成 Forge Agent 自动 PR 的真实闭环：冻结 Issue、
运行 Agent、独立隐藏验收、确定性 commit/push/create PR，并完整保留失败恢复证据。

## 冻结案例

- Issue：`https://github.com/Napabana/pr-test/issues/4`
- 基线：`main@9fc0579`
- 任务：为 `calculator.py` 增加 `clamp(value, lower, upper)`，保留既有行为并补测试。
- 限制：只允许修改 `calculator.py`、`tests/test_calculator.py`；新增注释/docstring 使用中文，
  能清晰单行表达的代码不无故拆行。
- 隐藏验收：`evals/pr_test_issue_4_verifier.py`，不进入 Agent History；先运行目标仓库完整
  pytest，再检查边界值、非法区间异常文本、既有函数和中文 docstring。
- 基线验收：原有 10 tests passed，但因不存在 `clamp` 返回失败，证明 verifier 能拒绝未改代码。

## 真实运行证据

| 次数 | Run / Trace | Agent 结果 | Steps | Tokens | 时间 | 直接原因 | 交付 |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| 1 | `80b7bd15_20260915_035408` | failed | 10 | 56,481 | 40.1s | test 工具误在 Forge cwd；后续 shell 测试虽 15 passed，Completion Guard 仍保留失败 | 未执行 |
| 2 | `712e643c_20260915_040004` | gave_up | 5 | 19,537 | 22.9s | 两次写入后 provider 返回空内容 | 未执行 |
| 3 | `011e36dd_20260915_040134` | failed | 9 | 44,621 | 45.8s | test 工具 15 passed，但 Agent 自行 commit 后工作区变干净，被 Completion Guard 判为无修改 | 未执行 |
| 4 | `297cb818_20260915_040600` | success | 8 | 47,454 | 40.3s | test 工具 15 passed，Completion Guard 与隐藏验收均通过 | PR #5 |

所有失败运行均在 Agent 状态失败后令 Acceptance 为 skipped，commit/push/create PR 门保持
关闭；对应工作目录与 JSONL Trace 未覆盖。第二次的 provider 空响应只作为失败样本记录，
没有把偶发错误包装为代码修复成果。

## 从案例反推的最小修复

- `entry/github_issue.py` 调用现有 `_build_registry` 时传入目标 `worktree_path` 与 `workspace`，
  让相对路径的 shell/test/git 和文件工具都落在目标仓库。
- 自动 PR 的 registry 移除 `git_add`、`git_commit`，使 Agent 无法在独立验收前改变提交
  基线；commit 继续由交付层唯一负责。
- `tests/test_github_issue_delivery.py` 增加一个契约节点，同时断言目标路径接线与提交工具
  不可见。WSL 最终重跑：`1 passed in 2.25s`。

## 成功交付

- Agent：success；Acceptance：passed；Delivery：delivered。
- 目标测试：15 passed in 0.41s（成功 Trace 的 `test` Observation）。
- 分支：`agent/fix-issue-4-1789445158`。
- commit：`0c5b108510b765df2e0676e870ad48c4e2de8102`，固定交付消息
  `fix: resolve issue #4`。
- diff：2 files changed，35 insertions，3 deletions。
- PR：`https://github.com/Napabana/pr-test/pull/5`，用户已人工合并；Merge Commit 与
  `main` HEAD 均为 `f5ad77c739e21efcd65e6e6524f320e3325a96f7`，Issue #4 已关闭。
- remote 保持 SSH `git@Napabana:Napabana/pr-test.git`；Token 只由环境提供，未写入 remote。

## 主调用链

```text
Issue #4 -> RunRequest/AcceptanceContract -> ExecutionRunner -> Agent.run
  -> ReAct: file_read/file_write/test -> Completion Guard
  -> Agent success -> hidden verifier -> Acceptance passed
  -> delivery: commit -> SSH push -> create PR -> Delivery delivered

任一 Agent/Guard 失败 -> Acceptance skipped -> delivery gate closed -> 保留本地 artifact/Trace
隐藏验收失败       -> Acceptance failed  -> delivery gate closed -> 不 push、不创建 PR
```

Reflection 在第一次错误测试后触发，但模型改用 shell 测试，无法覆盖 Completion Guard 所依据
的 test 状态；这正是首个 cwd 缺口的诊断证据，而不是成功链的一部分。

## 90 秒 case study

我先在专用测试仓库冻结了带路径限制和隐藏断言的 clamp Issue，并确认原有测试通过但隐藏
验收失败。第一次真实运行虽然生成了正确代码，却暴露 test 工具运行在 Forge cwd；修复为
复用 `_build_registry` 的目标目录参数。后续运行又发现 Agent 能在验收前自行 commit，导致
Completion Guard 把干净工作区误判为无修改，于是从自动 PR registry 移除提交工具，把提交
权收回交付层。最终 Agent 8 步完成，目标 15 个测试和仓库外 verifier 均通过，交付层生成
固定 commit、SSH push 并创建 PR #5。这个案例能证明门禁、失败保留和真实 PR 链路，但单个
成功样本不能证明成功率提升、Token 降低或通用性能收益。

## 当前边界

- 本案例没有启用 Docker、Worktree isolate 或人工 Permission，不能据此声称三层隔离已被
  真实 PR 同时验证。
- 没有运行 Repo Map 或 Compaction 消融，不报告 Recall@K、MRR、pass@1 或收益百分比。
- 模型曾把 `finish` 误当工具调用；下一轮能恢复为结构化 finish，因此本批没有扩展 tool
  schema 或 multi-tool call。
- PR 由用户人工审阅合并；Forge 仍未实现 auto-merge，且该能力继续后置。
