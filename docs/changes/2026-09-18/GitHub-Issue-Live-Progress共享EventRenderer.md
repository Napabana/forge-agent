# GitHub Issue Live Progress / 共享 Event Renderer

## 本轮目标

让 GitHub Issue 入口像 CLI 和 Chat 一样实时展示 Agent 执行进度，同时避免在
`entry/github_issue.py` 复制一套 LLM streaming 或事件打印逻辑。统一链路为：

```text
EventLog
  -> shared RunEventRenderer
       -> CLI
       -> Chat
       -> GitHub Issue
```

最低展示契约包括 step、action、tool、observation status、acceptance 和 delivery；
reasoning streaming 作为独立开关，不属于生命周期进度的必要条件。

## 实现

### 共享 renderer

- 新增 `entry/event_renderer.py::RunEventRenderer`，作为只读 EventLog observer。
- 统一渲染 `ACTION`、`OBSERVATION`、`ACCEPTANCE`、`DELIVERY`、任务终态、Reflection
  和 Context Compaction 标记。
- observer 异常仍由 EventLog 隔离，不改变 Agent、验收或交付结果。
- renderer 只保存单次显示所需的 pending message 和 streamed text，不进入 Agent History，
  不引入第二套执行状态机。

### 三入口接线

- CLI direct 从“运行结束后 replay 打印”改为通过 `ExecutionRunner.run(on_event=...)`
  实时消费事件。
- Chat 删除原有独立事件分支实现，改为每个 `ChatSession` 持有独立 renderer；保留
  `_print_event_live` 作为委托给共享 renderer 的兼容包装。
- GitHub Issue 把同一个 renderer 同时传给 Runner 与 `_record_delivery_trace`：
  acceptance 在 Runner 阶段实时显示，delivery 在重新打开 Trace 追加时继续使用同一 observer。

### isolate 事件边界

- `ExecutionRunner` 现在把 `on_event` 继续传给 `orchestrate_run`。
- orchestrator 用一个 observer 同时分发 UI callback 与既有 AgentBus queue，未创建第二份日志。
- isolate 结束后追加的 acceptance / run termination 也会重新挂载同一个 observer。

### reasoning streaming

- CLI run 与 Chat 新增 `--reasoning-stream/--no-reasoning-stream`。
- 未显式设置时继续跟随既有 `--stream`，保持默认行为兼容；显式设置后可独立控制
  thought callback 与最终文本 callback。
- GitHub Issue 新增同名开关，默认关闭 reasoning，但生命周期事件始终实时展示。

## 回归覆盖

- `tests/test_event_renderer.py`：固定 step/action/tool/observation/acceptance/delivery
  输出，以及 reasoning 默认隐藏、显式开启后可见。
- `tests/test_cli_isolate.py`：固定 isolate 将共享 observer 传入 orchestrator。
- `tests/test_day6.py`：固定 CLI direct 实时显示 action 和 acceptance。
- `tests/test_chat.py`：固定 reasoning 与 message streaming 可独立组合。
- `tests/test_stream.py`：固定 CLI、Chat、GitHub Issue 都暴露 reasoning-stream 开关。
- `tests/test_github_issue_delivery.py`：固定 GitHub Issue Runner observer 接线，以及 delivery
  追加事件能被同一 observer 收到。
- `tests/test_structured_compaction.py`：Chat 的 Context Compaction 显示改由共享 renderer 提供。

## 验证结果

WSL `Ubuntu-22.04-Recovered`、项目虚拟环境：

```text
定向入口/renderer 回归：104 passed in 13.52s
Trace/lifecycle/stream 回归：123 passed in 41.05s
reasoning 新增契约复跑：40 passed in 5.78s
全量 pytest：822 passed, 14 skipped, 2 warnings in 118.33s
```

两条 warning 都来自 AnyIO 在 Python 3.11 下调用 `Task.cancel(msg)` / `Future.cancel(msg)`
的 deprecation warning，与本轮变更无关。相关 13 个 Python 文件静态编译通过；
`git diff --check` 无补丁格式错误，仅有仓库既有 LF/CRLF 提示。

## 边界与工作区状态

- 未实现 Web UI、SSE 或远程 progress transport；本轮只统一终端产品入口的 EventLog observer。
- reasoning 输出仍由 provider streaming 能力决定；关闭 reasoning 不影响 lifecycle progress。
- 没有修改 Trace schema、EventType、Agent loop、验收规则或交付门禁。
- 用户原有 `config/default.yaml` 修改未触碰；B1/B2 fixture、`evals/results` 和 stash 均未处理。
- 当前分支为 `dev`。

