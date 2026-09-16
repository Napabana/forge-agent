# 项目级 Plugin 精简

## 本轮目标

仅在 Forge Agent 项目内关闭 Office/文档与求职相关 Plugin，同时保留 `ponytail`。

## 行为变化

- 关闭 `documents@openai-primary-runtime`。
- 关闭 `pdf@openai-primary-runtime`。
- 关闭 `spreadsheets@openai-primary-runtime`。
- 关闭 `presentations@openai-primary-runtime`。
- 关闭 `template-creator@openai-primary-runtime`。
- 关闭 `asu-skills@asu`。
- 未添加 `ponytail@ponytail` 的禁用配置，因此继续沿用用户级启用状态。
- 保留原有 `sandbox_workspace_write.network_access = true`。

## 修改文件

- `.codex/config.toml`
- `AGENTS.md`
- `docs/changes/2026-09-15/项目级Plugin精简改动内容.md`

## 验证

- 使用 Python 标准库 `tomllib` 解析 `.codex/config.toml`，确认 TOML 语法和六项禁用值。
- 本轮未修改业务代码，因此未运行项目测试。

## 已知边界

- 项目需处于 trusted 状态，项目级配置才会生效。
- 当前任务已加载的能力不会即时缩减，建议新开 Codex 任务验证。
