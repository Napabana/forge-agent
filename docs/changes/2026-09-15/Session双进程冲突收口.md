# Session 双进程冲突收口

## 本轮目标

保持 Session 冲突 fail-closed，为 CLI 提供可理解的恢复提示，并用两个独立进程验证
文件锁和 revision 契约；不实现自动合并或覆盖。

## 行为变化

- `session.run_round()` 抛出 `ChatSessionConflict` 时，CLI 不再只显示普通 Error。
- CLI 明确说明另一进程已保存更新、本轮 Session 未保存，并提示工具副作用可能已发生。
- 用户可以执行 `/resume <session_id>` 重载磁盘最新状态，或执行 `/new` 分开继续。
- 冲突仍向存储层保持 fail-closed，不自动覆盖、合并或重放工具。

## 修改文件

- `entry/cli.py`：在实际交互边界新增 `ChatSessionConflict` 专用提示。
- `tests/test_session_store.py`：新增两个 spawn 进程同时保存同一 revision 的契约测试。
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`、`AGENTS.md`：同步真实状态。

## 测试

WSL 默认发行版，环境 `source ~/.venvs/forge-agent/bin/activate`：

```text
python -m pytest -q tests/test_session_store.py
11 passed in 3.49s

python -m pytest -q tests/test_chat.py::TestChatCommand::test_chat_command_registered tests/test_chat.py::TestChatCommand::test_chat_listed_in_root_help
2 passed in 1.54s
```

两个子进程都先加载相同 revision，父进程再同时释放保存信号。真实结果为一个进程保存
成功、另一个进程收到 `ChatSessionConflict`，两个进程退出码均为 0。没有失败节点需要
重跑。两个 CLI 节点用于确认本次入口修改可以正常加载，未运行全量测试。

## 已知边界

- 当前不自动合并两份 Session，也不自动重放冲突轮次。
- 工具调用可能早于 checkpoint，因此 CLI 明确提示副作用可能部分发生。
- 本批只改善 `run_round()` 主交互路径；已有 `/rename` 仍沿用通用 Session 错误提示。
- 没有新增 SQLite 迁移、树形 Session、MCP、多 Agent 或性能结论。

## 保护状态与下一步

`config/default.yaml` 未修改，`stash@{0}` 保留；本地 Markdown 不强制暂存。下一批为
实施计划第 12.2 节 H-独立验收，需先确认三个拟修改文件及理由。
