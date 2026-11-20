# 模型适配与计量

统一模型消息、工具 schema、流式输出、预算、重试分类和用量。

## 怎么使用

在模型档案选择协议，代码通过 `create_backend_from_config` 构造适配器。

## 从哪里读代码

- [`base.py`](base.py)
- [`router.py`](router.py)
- [`request_budget.py`](request_budget.py)
- [`usage.py`](usage.py)
- [`cache_audit.py`](cache_audit.py)

## 预期效果

上层用同一消息和流式事件类型对接不同模型协议。请求预算和统一用量字段为运行记录与评测提供可比较的数据。

## 设计文档

[配置、模型与用量](../docs/design/configuration.md)
