# Chat / Direct 默认 CWD 统一修复

## 本轮目标

修复 Chat 和 direct run 的目标仓库与 process tools 默认工作目录未统一绑定的问题：文件工具已经使用目标 repo 作为 workspace，但 Shell、Test、Git 在 LLM 未显式传入 `cwd` 时仍会继承 Forge Agent 进程目录。

## 根因

`entry.cli._build_registry` 原来把 `worktree_path` 同时解释为 Shell/Test/Git 的 `default_cwd` 和 File tools 的 workspace fallback。isolate 会传入真实 worktree，因此表现正常；GitHub Issue 入口通过把普通目标仓库传成 `worktree_path` 临时规避。CLI direct 和 Chat 只传 `workspace`，process tools 因而得到 `default_cwd=None`，最终退回 Forge Agent 的 process cwd。

## 修改内容

- `entry/cli.py`：将 registry 参数 `worktree_path` 替换为独立的 `default_cwd`；`workspace` 不再从 `default_cwd` 或 worktree 隐式推导；CLI direct 与 Chat 都显式传入目标 repo。
- `agent/orchestrate.py`：registry builder 显式接收 `default_cwd`、`workspace`；isolate 同时把当前 worktree 传给两个参数。
- `entry/github_issue.py`：目标 clone 不再伪装成 `worktree_path`，改传 `default_cwd=local_path`。
- 三个 eval 入口、`scripts/m4_demo.py` 和测试 fake builder 同步新契约。

| 入口 | process tools `default_cwd` | file tools `workspace` |
| --- | --- | --- |
| CLI direct | target repo | target repo |
| Chat | target repo | target repo |
| GitHub Issue | cloned target repo | cloned target repo |
| isolate | isolated worktree | isolated worktree |

本轮没有修改 Shell、Test、Git 工具的覆盖规则；LLM 显式传入的 `cwd` 仍优先于 registry default。

## 回归覆盖

- `tests/test_cli_isolate.py`：固定 `default_cwd` 与 `workspace` 可独立设置，全部 process tools 和文件工具分别获得对应值。
- `tests/test_day6.py`：固定 CLI direct 把目标 repo 同时传给两个参数。
- `tests/test_chat.py`：固定 Chat registry 中 Shell 的默认 cwd 为目标 repo。
- `tests/test_orchestrate.py`：固定 isolate 的两个参数都指向同一个 worktree。
- `tests/test_github_issue_delivery.py`：GitHub Issue 接线契约更新为 `default_cwd + workspace`。

## 验证

- Codex 环境使用系统 Python 和随附 Python 尝试运行 pytest 时均缺少 `pytest`；没有把该环境失败记为代码失败。
- 用户随后在本地执行本轮测试并明确确认全部通过；用户未提供 passed 数量、完整 stdout 或耗时，因此本文不补造数字。
- 额外静态编译检查覆盖 13 个本轮相关 Python 文件，全部通过。
- `git diff --check` 未发现补丁格式错误；仅出现仓库既有 Windows LF/CRLF 提示。

## 边界与工作区状态

- 没有重构 `ExecutionRunner`，没有增加新抽象或依赖。
- 没有修改 B1/B2 fixture 或 `evals/results`。
- 工作区原有 Smoke Test / Model-aware Token Budget 和 `config/default.yaml` 未提交修改继续保留，本轮未覆盖或还原。
- 当前分支仍为 `dev`，现有 stash 未处理。

