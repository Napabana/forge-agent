# ConversationHistory 测试契约修正

## 本轮目标和结论

修正 `test_day5.py` 中仍按旧滑动窗口语义编写的测试。当前 `ConversationHistory.max_messages` 只是兼容旧配置的提示，逻辑历史由存储层完整保留，因此四条消息都应存在。

## 实际修改

- 修改 `tests/test_day5.py`：将测试重命名为 `test_history_keeps_messages_beyond_max_hint`。
- 断言消息数量为 4，并验证消息顺序完整保留。
- 未修改生产代码、评测 fixture 或历史结果。

## 验证

```bash
wsl bash -lc 'cd /mnt/e/2806/forgeAgent/forge-agent && source ~/.venvs/forge-agent/bin/activate && python -m pytest tests/test_day5.py::TestConversationHistory::test_history_keeps_messages_beyond_max_hint -q'
```

结果：`1 passed in 1.58s`。

Windows 默认 Python 未安装 pytest，直接运行 `python -m pytest` 得到 `No module named pytest`；不影响 WSL 虚拟环境中的实际验证。

## 工作区

- 分支：`dev`
- 状态：本轮修改 `tests/test_day5.py`，并新增本日志文件。
- 未执行 commit、push 或 stash 操作。
