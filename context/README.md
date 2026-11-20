# 上下文与仓库导航

维护完整历史、模型视图、上下文预算、RepoMap 和归档来源。

## 怎么使用

由 Agent 构建下一轮模型输入，可通过 Runtime 命令浏览归档。

## 从哪里读代码

- [`history.py`](history.py)
- [`token_budget.py`](token_budget.py)
- [`repo_map.py`](repo_map.py)
- [`incremental_repo_map.py`](incremental_repo_map.py)
- [`compaction.py`](compaction.py)
- [`archive.py`](archive.py)

## 预期效果

模型在输入预算内看到近期消息和相关代码。裁剪和压缩保留来源，后续仍可回查关联的原始内容。

## 设计文档

[上下文与仓库导航](../docs/design/context.md)
