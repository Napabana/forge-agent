# P0-1、P0-2 与 Trace v2 最小闭环

## 本轮目标

在不改变 `Agent.run()` 同步模型、不引入新依赖和不开始 compaction/MCP/多 Agent
的前提下，收口 `prepare_next_turn`、Tool Hook/参数校验/错误分类，并建立最小
JSONL Trace v2。

## 行为变化

- `prepare_next_turn` 由 `AgentConfig` 注入；第一轮跳过，只在完整工具 turn 后、
  下一次 `_build_messages()` 前同步调用。返回消息只影响下一轮；异常转为失败结果；
  callback 前后都检查取消。
- Tool 执行顺序固定为参数校验、Pre Hook、Permission、Tool、Post Hook、
  Observation。Pre Hook 明确阻止或抛错时 fail-closed；Post Hook 仍只观察。
- Tool 错误增加稳定的 `error_type`，并传播到 `Observation`。本轮实现了 unknown tool、
  invalid arguments、permission denied、hook blocked/failed 和未捕获 tool exception；
  timeout/infrastructure 的全工具映射仍待后续补齐。
- `AgentConfig.hooks` 同时接入默认 `ToolExecutor` 和 `orchestrate_run()` 创建的
  executor，不使用全局 Hooks 注册表。
- Trace v2 继续写入现有 JSONL。prepare、LLM、tool 都有 started/finished/failed
  生命周期事件；LLM 另有 retry 事件。公共字段包含 schema version、run/turn/step/span
  关联字段，阶段事件记录耗时、usage、结果字节数或错误分类。
- 新 Trace 不记录 prompt 文本、工具参数、工具输出或模型原始响应，只记录计数和元数据。
  既有 `ACTION.raw_content` 的可配置持久化和既有 Permission 参数脱敏不在本轮处理。
- `summarize_run()` 增加 prepare/LLM/tool 次数、重试次数、分类耗时和错误计数。

## 修改文件

- `agent/core.py`
- `agent/event_log.py`
- `agent/orchestrate.py`
- `agent/task.py`
- `harness/__init__.py`
- `harness/executor.py`
- `harness/hooks.py`
- `tools/base.py`
- `tests/test_prepare_next_turn.py`
- `tests/test_harness.py`
- `tests/test_trace_v2.py`
- `TODO-P0-P1.md`
- `AGENTS.md`

未修改 `config/default.yaml`。

## 测试证据

计划使用用户的 WSL 环境：

```text
source ~/.venvs/forge-agent/bin/activate
pytest tests/test_prepare_next_turn.py tests/test_harness.py tests/test_trace_v2.py -q
```

但本轮 WSL 启动在执行 pytest 前失败：先出现 `Wsl/Service/E_ACCESSDENIED`，授权后
仍为 `Wsl/Service/CreateInstance/E_UNEXPECTED`。这不是测试失败。

随后用 Windows Python 3.12.14 和项目 `.venv` site-packages 运行相同范围：首次
收集 29 项，结果 **28 passed、1 failed（1.62s）**。失败原因是旧 Permission 测试
调用未注册的 `file_read`，与新增“schema 校验先于 Permission”契约冲突。给测试
fixture 注册 fake `file_read` 后，仅重跑失败节点，结果 **1 passed（0.25s）**；修正
Trace 测试的 Task 关键字构造后，仅重跑 `tests/test_trace_v2.py`，结果
**1 passed（0.57s）**。

为减少重复等待，本轮没有再次重跑全部 29 项，也没有运行全量测试。因此准确结论是：
首次通过的 28 项以及两个修正后的定向节点分别通过，不宣称全量回归通过。

## 已知边界与下一步

- P0-1 仍需集中补异常、callback 前取消、callback 中取消、共享 Chat History 与
  Repo Map 刷新的边界测试；默认 `None` 回归也留到 P0 最终定向批次。
- P0-2 仍需补齐 timeout/infrastructure/provider/retry-exhausted 映射，以及 Pre/Post
  Hook 异常、兼容返回值和各产品入口的集成证据。Post Hook 异常目前有 warning，
  但尚未成为独立 Trace 事件。
- Trace v2 仍缺 Prompt 分区估算、取消阶段/延迟、敏感字段 redaction 测试、恢复成功/
  false-finish 汇总和真正的父子 span 树。`session_id`/`parent_span_id` 已保留传播入口，
  当前单次 `Agent.run()` 没有上层 Runner 提供它们，因此默认是 `null`。
- 下一步按 TODO 进入 P0 集中测试与上述缺口收口；完成后再开始 P1-1 Runner。
