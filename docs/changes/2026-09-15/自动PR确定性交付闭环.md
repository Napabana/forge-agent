# 自动 PR 确定性交付闭环

## 本轮目标

完成实施计划 12.2 第 6 项：把 Runner 的独立验收状态接到 GitHub Issue 自动交付入口，
并用完全本地的 Git remote 与伪 GitHub 客户端验证失败保留和重试边界。

## 行为变化

- 自动 PR 必须显式提供 `--verify-command`；命令以参数数组运行，不经过 shell，也不进入
  Agent History。
- 只有 Agent 成功且独立验收为 `passed` 时才允许 commit、push 和 create PR。
- 自动交付在 Agent 运行前要求仓库干净，避免把用户原有修改混入提交；`--no-pr` 不受此
  交付门禁影响。
- 无 diff、Agent/验收阻断、commit、push、PR 失败均留下明确 `delivery_status`。
- push 失败保留干净的本地 commit；PR 失败后重试会比较本地 HEAD 和远端分支 SHA，若已
  推送则跳过重复 commit 和 push。

## 修改文件

- `entry/github_issue.py`：增加验收命令适配、交付门禁、阶段状态和同仓库重试判断。
- `tests/test_github_issue_delivery.py`：增加本地仓库、bare remote 和 fake PR 客户端契约。
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`、`AGENTS.md`：同步真实完成状态。

没有新增依赖，也没有提取新的交付模块；现有函数足以覆盖当前单一入口。

## 定向测试

WSL 默认发行版，环境：`source ~/.venvs/forge-agent/bin/activate`。

```text
python -m pytest -q tests/test_github_issue_delivery.py
4 passed in 1.67s

python -m pytest -q \
  tests/test_day6.py::TestGitHubIssueLogic::test_fetch_issue_no_token_raises \
  tests/test_day6.py::TestGitHubIssueLogic::test_fetch_issue_mocked \
  tests/test_day6.py::TestGitHubIssueLogic::test_create_pr_mocked
3 passed in 2.04s
```

没有失败节点，因此没有重跑；按约定未运行全量测试。

## 已知边界

- 测试没有访问真实 GitHub、没有 push 到真实 remote，也没有创建真实 PR。
- 幂等边界是对同一保留仓库、同一分支和固定 commit message 的重试；重新执行完整 CLI
  会创建时间戳分支，不宣称跨全新 clone 自动续跑。
- 本轮只完成代码级交付契约，不能替代冻结 Issue、真实 Agent Trace、消融数字和最终
  case study；没有新增任何性能或成功率结论。
- `config/default.yaml`、SSH remote 和现有 stash 均未修改。
