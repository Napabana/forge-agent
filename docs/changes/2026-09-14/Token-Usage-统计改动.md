# 2026-09-14 Token Usage 统计改动

## 本轮目标

修正不同 LLM SDK 返回的 Token Usage 口径，并让 Forge Agent 能记录每次调用、每轮
对话和整个 Chat Session 的用量。在终端的轮次结束信息与 `/stats` 中展示统计结果，
为后续 Context Compression、Prompt Cache 和 Agent Evaluation 实验提供基础指标。

本轮没有实现费用估算，也没有开始 P0-1、MCP、多 Agent、multi-tool call、compaction
或 Repo Map 重构。

## 统一统计口径

- `input_tokens`：完整输入 Token，已经包含缓存命中与缓存写入部分。
- `cached_tokens`：`input_tokens` 中命中缓存的子集。
- `cache_write_tokens`：`input_tokens` 中写入缓存的子集。
- `output_tokens`：完整输出 Token，已经包含 reasoning 部分。
- `reasoning_tokens`：`output_tokens` 中的 reasoning 子集。
- `total_tokens = input_tokens + output_tokens`。
- `cached_tokens`、`cache_write_tokens`、`reasoning_tokens` 不再重复加到 total。
- 缓存命中率为 `cached_tokens / input_tokens`；输入为 0 时显示 0%。

例如 input 72,073、cached 53,248、output 1,413 时，总量为 73,486，缓存命中率约
为 73.9%，而不是把 cached 再加一次。

## 实现内容

### LLM 响应与 Provider 归一化

- 新增统一的 `TokenUsage` 与 `SessionUsage` 数据结构，负责字段校验、累加、序列化和
  缓存命中率计算。
- `LLMResponse` 持有结构化 usage，同时保留旧构造方式的兼容入口。
- OpenAI Chat Completions 解析 prompt、cached、completion 和 reasoning usage。
- OpenAI Chat streaming 请求 `stream_options.include_usage`，并读取最终空 `choices`
  usage chunk。
- OpenAI Responses 解析 input、cached、output 和 reasoning usage。
- Anthropic 在 backend 边界将普通输入、cache read、cache creation 合并成统一的完整
  `input_tokens`，同时保留两个缓存子集。

### 调用、轮次与 Session 聚合

- Agent 在每次 LLM Response 返回后累计 usage，并记录真实 `llm_calls`。
- EventLog 的 Action 事件写入单次 LLM 调用 usage，便于后续实验逐调用分析。
- Run 结果携带本轮聚合 usage。
- Chat Session 保存并恢复累计 usage；旧 session 缺少该字段时按零值兼容。

数据流为：

```text
LLM backend response
  -> TokenUsage
  -> Agent 本轮累计 / EventLog 单次记录
  -> Chat SessionUsage 累计与持久化
  -> 轮次结束信息、/stats、退出摘要
```

### 终端展示

- 每轮 Chat 结束后显示 calls、total、input、cached、cache-write、output、reasoning、
  cache-hit 和耗时。
- 用户输入 `/stats` 时显示当前 Session 累计值。
- 退出 Chat 时显示 Session 汇总。

示例格式：

```text
Round 1 · 5 steps · 73,486 tokens
(5 calls; input 72,073, cached 53,248, cache-hit 73.9%,
cache-write 0, output 1,413, reasoning 0) · 147.3s
```

## 修改文件

生产代码：

- `llm/usage.py`：新增统一 usage 模型与 Session 聚合。
- `llm/base.py`：让 `LLMResponse` 携带结构化 usage，并兼容旧调用。
- `llm/openai_compat.py`：OpenAI Chat Completions 非流式/流式 usage 解析。
- `llm/openai_responses.py`：OpenAI Responses usage 解析。
- `llm/anthropic_backend.py`：Anthropic usage 归一化。
- `agent/core.py`：聚合每次 LLM 调用的 usage。
- `agent/event_log.py`：Action 事件记录单次 usage。
- `agent/task.py`：Run 结果暴露聚合 usage。
- `agent/session.py`：Chat Session 累计、持久化和恢复 usage。
- `entry/chat.py`：轮次结束、`/stats` 与退出时的终端展示。

测试代码：

- `tests/test_llm_usage.py`：统一口径、累加、序列化和命中率。
- `tests/test_day4.py`：Agent 聚合与 EventLog usage。
- `tests/test_openai_responses.py`：Responses usage 映射。
- `tests/test_stream.py`：Chat Completions streaming 最终 usage chunk。
- `tests/test_session_store.py`：Session usage 持久化与恢复。

`config/default.yaml` 的现有变更属于用户配置，本轮未修改、未还原。

## 验证结果

- 最新口径的 8 个直接相关测试全部通过：首次运行 6 passed，另 2 个因测试预期仍按
  旧口径而失败；修正断言后，这 2 个也通过。
- 此前 usage/session 定向测试结果为 11 passed（19.43s）。
- 四组 Harness 回归结果为 84 passed、1 failed；失败是既有 Repo Map 测试的
  monkeypatch 函数不接受 `force_refresh` 参数，与本轮 usage 改动无关。
- 曾误启动一次全量测试，已按要求中止，因此不把它报告为完整回归结果。
- Codex 宿主当时无法启动 WSL（`CreateInstance/E_UNEXPECTED`）；上述定向测试使用
  Windows Python 3.12 与项目 site-packages，未调用真实模型。用户已确认其 WSL smoke
  测试通过。
- 本轮新增日志与 `AGENTS.md` 未检出尾随空格；当前全工作区 `git diff --check` 会报告
  `config/default.yaml:35` 的既有尾随空格。该文件是需保留的用户配置，本轮未处理。

## 已知边界

- 尚未实现 `estimated_cost_usd`。费用依赖 provider/model 的价格表与缓存计价规则，
  当前先保留准确的原始 Token 指标。
- 不同中转站必须按实际响应字段进行映射；当前 total 的统一语义固定为
  `input + output`，不会因 provider 子字段存在而重复计数。
- 当前统计是后续实验的观测基础，不代表已经完成 Context Compression、Prompt Cache
  策略或 Agent Evaluation 框架。
