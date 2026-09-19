# MCP 清理契约测试替身修复

## 问题

Chat 与 GitHub Issue 入口新增统一资源清理调用后，测试替身缺少 `close()`，导致测试在业务断言完成后的清理阶段抛出 `AttributeError`：

- `FakeSession` 缺少 `close()`。
- `AssertRunner` 缺少 `close()`。

## 最小修复

为两个测试替身补充空实现 `close()`，与生产对象的资源生命周期契约保持一致。未修改生产逻辑。

## 验证

```text
python -m pytest -q tests/test_chat.py::TestChatCommand::test_chat_sandbox_injects_runtime_and_cleans_up tests/test_github_issue_delivery.py::test_issue_registry_uses_target_repo
3 passed

python -m pytest -q tests/test_chat.py tests/test_github_issue_delivery.py
25 passed
```

`git diff --check` 通过。未运行真实 LLM 或交付操作。
