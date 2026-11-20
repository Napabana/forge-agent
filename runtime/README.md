# 工作树运行时

管理隔离 Git worktree 的创建、续接、检查和清理。

## 怎么使用

用 `forgeAgent run --repo . --isolate --task '任务'` 运行；查看结果中的 worktree 产物。

## 从哪里读代码

- [`worktree.py`](worktree.py)

## 预期效果

任务改动落在独立工作树，中断后的任务可以重新绑定。结果可交付保留的工作树，清理策略根据任务结果决定是否移除。

## 设计文档

[隔离、验收与 GitHub 交付](../docs/design/isolation-acceptance.md)
