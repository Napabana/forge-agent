# no-pr 兼容契约改动内容

## 本轮目标

固定 GitHub Issue 两种运行模式的提交权边界：自动 PR 由独立验收后的交付层统一 commit，
`--no-pr` 则保持 Agent 原有 `git_add`/`git_commit` 工具能力。

## 修改内容

- 仅修改 `tests/test_github_issue_delivery.py`。
- 将已有 registry 接线测试参数化为 `create_pr=True/False` 两个场景。
- 两个场景都验证工具默认目录与 workspace 为目标仓库。
- 自动 PR 场景断言提交工具被移除；`--no-pr` 场景断言两个提交工具完整保留。

测试证明现有生产代码已满足契约，因此没有修改 `entry/github_issue.py`，也没有新增 helper、
依赖或模式分支。

## 定向测试

WSL 环境：`source ~/.venvs/forge-agent/bin/activate`。

```text
python -m pytest -q tests/test_github_issue_delivery.py::test_issue_registry_uses_target_repo
2 passed in 2.34s
```

没有失败节点，因此没有重跑；按约定未运行全量测试。

## 已知边界与下一步

- 本契约只固定工具可见性，不改变自动 PR 的验收与交付实现。
- `--no-pr` 允许 Agent 自行 commit 是兼容旧行为，不代表自动 PR 放宽提交权。
- 下一优先级先统计现有 JSONL 中 provider 空响应与 `finish` 误调用频率；无重复证据前不
  编写恢复机制。
- `config/default.yaml`、Forge SSH remote 与 `stash@{0}` 均未修改。
