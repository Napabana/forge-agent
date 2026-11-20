# 配置与凭据

将运行配置、模型档案、预算和系统凭据引用加载为类型化对象。

## 怎么使用

在用户配置中选择模型和功能，入口通过 `load_config` 加载。

## 从哪里读代码

- [`schema.py`](schema.py)
- [`credentials.py`](credentials.py)
- [`default.yaml`](default.yaml)

## 预期效果

各入口使用一致的模型、预算和功能开关。配置记录凭据引用，具体密钥由环境变量或系统凭据存储按需解析。

## 设计文档

[配置、模型与用量](../docs/design/configuration.md) · [终端显示配置](../docs/design/terminal-ui.md)
