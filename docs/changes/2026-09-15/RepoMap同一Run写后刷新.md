# Repo Map 同一 Run 写后刷新

## 本轮目标

完成 G-一致性最小闭环：成功的文件写工具修改仓库后，使同一 `Agent.run()` 的下一 step
强制刷新 Repo Map。保持全量刷新，不实现增量索引或其他扩展。

## 行为变化

- `file_write`、`file_edit` 或 `edit` 返回成功后，Agent 失效当前仓库摘要。
- 下一 step 的 `_build_messages()` 通过现有 `force_refresh=True` 路径重新扫描仓库。
- 失败的写工具不失效摘要；非写工具仍复用当前缓存。

## 修改文件

- `agent/core.py`：在现有成功写工具分支调用 Repo Map 失效接口。
- `tests/test_repo_map_improvements.py`：新增同一 Run 创建文件后下一轮消息可见的最小
  契约测试。
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`、`AGENTS.md`：同步真实状态、
  测试结果和下一批确认点。

## 测试

WSL 默认发行版，环境 `source ~/.venvs/forge-agent/bin/activate`：

```text
python -m pytest -q tests/test_repo_map_improvements.py
10 passed in 1.94s
```

首次在受限执行环境中启动 WSL 返回 `E_ACCESSDENIED`，获准在 WSL 中运行同一命令后
测试通过。这是执行环境权限问题，不是测试节点失败，因此没有重跑任何 pytest 节点。

## 已知边界

- 当前是仓库级全量失效，没有增量解析、持久化索引或 repo-changed 事件。
- 契约测试覆盖创建新文件；删除、重命名和跨 Session 场景仍未单独覆盖。
- 未运行全量测试，也未产生 Recall@K、MRR、pass@1 或性能收益结论。
- `config/default.yaml` 和 `stash@{0}` 均未修改；本地 Markdown 按约定保持忽略状态。

## 下一步

按实施计划第 12.2 节，第二批为 G-EvalRunner 最小闭环。需先列出拟修改文件与理由，
等待用户确认后再修改第二批代码。
