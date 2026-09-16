# Token 安全 Clone 改动内容

## 本轮目标

修复 GitHub Issue 入口把 `GITHUB_TOKEN` 拼入 HTTPS clone URL、继而可能写入
`.git/config` 的风险，同时保持私有仓库 clone、push 和 PR 重试可用。

## 行为变化

- clone URL 固定为 `https://github.com/<owner>/<repo>.git`，不再包含 Token。
- 使用 Git 临时 `http.https://github.com/.extraHeader` 环境传递 Basic 认证；配置只对子进程
  生效，不写入仓库 remote。
- push 和 `ls-remote` 使用同一临时认证，避免私有 tokenless HTTPS remote 在交付或重试时
  失去认证。
- Git 子进程不继承明文 `GITHUB_TOKEN`，并移除 `GIT_TRACE*`、`GIT_CURL_VERBOSE`，降低
  HTTP 认证头进入调试输出的风险。
- 没有 Token 时保持公开仓库原有匿名 HTTPS 行为。

## 修改文件

- `entry/github_issue.py`：扩展 `_run_git(..., env=...)`，新增最小临时认证环境，并接到
  clone、push、远端 SHA 查询。
- `tests/test_github_issue_delivery.py`：新增成功和失败安全契约，使用假 Token，未读取真实值。

代码继续使用中文注释，能清晰单行表达的更新保持单行；未新增依赖或凭据管理抽象。

## 定向测试与冒烟

WSL 环境：`source ~/.venvs/forge-agent/bin/activate`。

```text
tests/test_github_issue_delivery.py::test_clone_uses_ephemeral_auth_and_tokenless_remote
tests/test_github_issue_delivery.py::test_clone_failure_does_not_expose_token
2 passed in 1.34s

tests/test_github_issue_delivery.py::test_push_failure_retains_local_commit
tests/test_github_issue_delivery.py::test_pr_retry_does_not_repeat_commit_or_push
2 passed in 1.50s

移除 Git 子进程明文 Token 后重跑唯一受影响安全节点：
1 passed in 1.12s
```

真实只读冒烟使用环境中的 Token clone 私有 `Napabana/pr-test`，结果：

```text
REMOTE_URL= https://github.com/Napabana/pr-test.git
TOKEN_IN_REMOTE= False
```

临时仓库由 `TemporaryDirectory` 自动清理；未创建分支、未 push、未修改远端。无失败节点，
按约定未运行全量测试。

## 已知边界

- Basic header 中包含 Token 的可逆编码，这是 GitHub HTTPS 认证所需；该值只存在于 Git
  子进程环境，不进入命令参数、remote 或 Forge 输出。
- 本轮真实冒烟只验证 clone；push/重试使用本地 bare remote 契约回归验证，没有再次修改
  `pr-test` 远端。
- 不实现系统 Credential Manager、auto-merge、模型协议恢复或新依赖。
- `config/default.yaml`、Forge SSH remote 与 `stash@{0}` 均未修改。
