# Completion Guard 运行日志隔离修复

## 问题

真实 `agent run` 将 EventLog 写入目标 Git 仓库的未忽略目录时，JSONL 在每一步持续追加。`repository_content_fingerprint()` 会把该日志视为测试后的仓库内容变化，导致已经通过测试的任务在 `FINISH` 时反复触发 `FINAL_STATE_UNVERIFIED`。

## 最小修复

- `config/schema.py`：无显式配置时默认使用 `~/.forge-agent/logs`，并在配置解析边界对 `agent.log_dir` 调用 `Path.expanduser()`；显式绝对路径保持不变。
- `context/repository_state.py`：内容指纹支持精确的 `excluded_paths`，Git 和 filesystem fallback 使用同一语义。
- `agent/core.py`：所有 Completion Guard 内容快照只排除本次 `EventLog.path.resolve()`，不忽略整个 `logs/` 目录，也不放宽测试后真实源码变化必须重测的契约。
- 用户现有 `config/default.yaml`（含模型/API 与绝对日志目录配置）未由本轮修改。
- `.gitattributes` 固定常用源码/文档格式为 LF，避免 Windows Git 与 WSL 共用工作树时把 CRLF 噪声带入暂存区；本地 Git 同时忽略实验 Gitlink 的 dirty 状态展示。

## 回归覆盖

- Git 仓库中本次运行日志继续增长，不改变排除后的内容指纹。
- 同仓库其他普通文件仍改变内容指纹。
- 日志位于目标 Git 仓库内部时，成功测试后可以正常 `FINISH`。
- 既有 Completion Guard 回归继续覆盖测试后真实修改、恢复初始状态、commit 元数据变化和失败后重测。

## 验证

按 `AGENTS.md` 在 WSL `Ubuntu-22.04-Recovered` 中激活 `~/.venvs/forge-agent` 后通过：

```text
pytest -q tests/test_repository_state.py \
  tests/test_agent_completion_guards.py \
  tests/test_day6.py::TestParseConfig \
  tests/test_day6.py::TestLoadConfig

49 passed in 6.62s

pytest -q

1081 passed, 14 skipped, 30 warnings in 141.13s
```

相关文件 `py_compile` 与 `git diff --check` 通过。全量 warnings 为既有 AnyIO/Python 3.11 弃用提示。

## 边界

- 只排除当前 EventLog 文件；其他日志、生成物或业务文件不会自动排除。
- 未调用真实模型或外部 API。
- 未处理现有 stash、未修改 Benchmark fixture 或历史 `evals/results`。
- 没有对全仓库执行换行重写，只重新规范化本轮交付文件的暂存内容。
