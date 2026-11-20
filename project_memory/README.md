# 项目记忆

分项目检索可复用知识，保存候选、来源与验收关系。

## 怎么使用

用 `forgeAgent memory status --repo .` 查看，用 `search`、`approve` 或 `reject` 管理。

## 从哪里读代码

- [`identity.py`](identity.py)
- [`runtime.py`](runtime.py)
- [`writer.py`](writer.py)
- [`transfer.py`](transfer.py)

## 预期效果

新任务可以检索同一仓库中已确认的约定、命令和路径线索。候选保留来源和验收信息，便于审核与复用。

## 设计文档

[项目记忆](../docs/design/project-memory.md)
