# 配置、模型与用量

终端显示使用独立的 `ui` 配置段。参数和降级规则见 [CLI 终端界面](terminal-ui.md)。

## 为什么实现

配置层把用户设置与模型、工具和运行时能力连接起来，Provider 适配器统一协议与用量语义。

## 怎么使用

安装项目后运行 `forgeAgent init`，或用 `forgeAgent --config PATH run --repo . --task '任务'`。`forgeAgent doctor` 检查环境，`forgeAgent config models` 查看模型档案。可选 `.[keyring]` 后使用 `init --keyring`。

## 怎样实现

`config/schema.py` 解析 YAML、环境引用、模型能力与运行开关。系统凭据通过 `config/credentials.py` 读取。`llm/` 提供 Anthropic Messages、OpenAI Chat Completions 和 OpenAI Responses 适配，统一消息、工具调用、流式输出、请求预算和用量。

## 预期效果

切换 Provider 时保持任务和工具行为稳定，并区分供应商返回用量、本地估算、缓存读取与未知值。

## 使用边界

凭据使用私有用户配置、环境变量或系统凭据库。实际模型窗口、协议及缓存字段取决于供应商返回和已配置能力。
