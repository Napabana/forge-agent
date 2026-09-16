# 真实 PR 合并后续规划改动内容

## 本轮目标

核验 `pr-test` 首个真实 PR 的合并结果，并在修改下一批生产代码之前，把后续事项按安全、
兼容、可靠性和证据价值排序写入持续计划。

## 已核验状态

- PR：`https://github.com/Napabana/pr-test/pull/5`。
- 状态：closed 且 merged。
- 合并时间：2026-09-15 04:19:50 UTC，即北京时间 2026-09-15 12:19:50。
- Merge Commit：`f5ad77c739e21efcd65e6e6524f320e3325a96f7`。
- `main` HEAD：`f5ad77c739e21efcd65e6e6524f320e3325a96f7`。
- Issue #4：closed。

以上信息通过 GitHub API 只读核验；没有执行 merge、删分支或修改远端内容。

## 后续优先级

1. 修复 Token-safe clone。当前 `clone_repo()` 会把 `GITHUB_TOKEN` 拼入 HTTPS URL，可能
   写入 `.git/config`；下一版必须使用临时认证环境，并保证参数、remote、异常和日志无 Token。
2. 补 `--no-pr` 兼容测试。自动 PR 应由交付层独占 commit，但普通 `--no-pr` 模式仍应保留
   Agent 的 `git_add`/`git_commit`。
3. 汇总 provider 空响应和 `finish` 误调用失败样本。先建立发生频率，再决定恢复机制。
4. 完成 Repo Map、Context Compaction 消融，以及隔离图和 Claim 账本。
5. auto-merge 后置。真实项目继续人工审阅；测试仓库也只在保护条件明确后单独实现。

## 下一批拟修改文件

- `entry/github_issue.py`：将 clone 认证从带 Token URL 改为不持久化凭据的临时 Git 环境。
- `tests/test_github_issue_delivery.py`：验证 Token 不进入 clone 参数、remote 或失败输出。

下一批不修改模型协议、不增加依赖、不实现 auto-merge，也不顺带调整 Repo Map 或
Compaction。按照 `AGENTS.md`，等待用户确认上述两个代码文件后再执行。

## Git 与测试

- 本轮只更新被 `.gitignore` 忽略的 Markdown，没有修改生产代码。
- 因无代码变化，本轮未运行测试。
- Forge 基线仍为 `dev...origin/dev`、HEAD `1b65f84`；既有未提交修改全部保留。
- `config/default.yaml`、SSH remote 与 `stash@{0}` 均未修改。
