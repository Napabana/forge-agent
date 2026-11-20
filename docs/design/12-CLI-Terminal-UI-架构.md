# 12. CLI Terminal UI：Chat 与 Run 统一终端体验设计

> 实施状态：Run、隔离 Run、Chat 和 GitHub Issue 已接入统一终端界面。当前组件、配置、命令与验证入口见 [CLI 终端界面](terminal-ui.md)。以下保留实施前的设计合同和验收要求。[GitHub 原文](https://github.com/Napabana/forge-agent/blob/c7a19f40d9575e49728f145dbac89855ce38dbd5/docs/design/12-CLI-Terminal-UI-%E6%9E%B6%E6%9E%84.md)
>
> 源码基线：Napabana/forge-agent，dev，HEAD = 3e595b966d5c98c23809582496b7915ac5e8ce58（2026-11-19）。
>
> 目标：统一 Chat、Run、isolate Run 和其他 ExecutionRunner 入口的终端视觉与事件呈现。本文描述目标体验和实现边界，不代表相关 UI 已经实现。

## 1. 设计目标

Forge Agent 已经能够通过 EventLog 和 RunEventRenderer 输出执行事件，也已经使用 prompt_toolkit 处理 Chat 输入，并在依赖中安装 Rich。本阶段不重新实现 Agent 执行逻辑，而是把现有终端输出升级为一致、稳定、可读的产品级 CLI UI。

核心目标：

1. **统一视觉语言**：Chat 和 Run 使用同一套颜色、状态、排版、代码、Diff、工具卡片和统计组件。
2. **Run 具备完整执行界面**：展示任务信息、当前阶段、工具执行、测试结果、恢复过程、验收和最终交付，不再只输出零散日志行。
3. **Chat 保持交互效率**：保留现有 PromptSession、命令补全、@path 补全和会话能力，改善消息、工具调用、状态栏和输入区域。
4. **稳定流式渲染**：模型文本、工具事件、Permission 确认和日志不能互相覆盖；窗口缩放时不产生残留字符。
5. **兼容普通终端和自动化**：非 TTY、CI、SSH、Windows Terminal、VS Code Terminal、日志重定向等场景有合理降级。
6. **执行与显示分离**：UI 只消费事件和流式文本，不改变 Agent、Tool、Permission、Trace、RunResult 或验收语义。
7. **不引入不必要依赖**：优先复用当前已安装的 Rich、Click 和 prompt_toolkit，不在第一版引入 Textual 等完整 TUI 框架。

本阶段的产品范围同时覆盖 **forgeAgent chat** 和 **forgeAgent run**。不能只把 Chat 改成漂亮的界面而让 Run 继续输出原始日志。

## 2. 参考 CLI 与设计取舍

本设计参考成熟 Coding Agent CLI 的交互方式，但不复制某个产品的完整布局。

| 参考产品/技术 | 值得采用的设计 | Forge 的落地方式 |
|---|---|---|
| OpenAI Codex CLI | Markdown 和 Diff 语法高亮、主题选择、长 Prompt 外部编辑器、可配置 alternate screen | Rich Markdown/Syntax、统一 Theme、Ctrl+G 调用 VISUAL/EDITOR；第一版默认 inline |
| Google Gemini CLI | 稳定输入区域、Sticky Header、窗口缩放稳定、减少闪烁、退出后保留终端历史 | Chat 输入区域保持稳定；Permission/当前工具标题清晰；默认不重绘整屏 |
| Claude Code | 可定制 Status Line，展示模型、目录、上下文、成本等；快捷键可配置 | Chat/Run 共用状态信息模型，展示 model、repo、branch、token usage、elapsed |
| Aider | Slash Commands、Diff、Tokens、Repo Map、Model、Editor 等命令入口 | 保留现有 Chat 命令，并增加 /diff、/tokens、/status 等可用入口 |
| Rich | Console 自动检测终端能力；Live/Status 用于局部动态展示；Panel、Table、Markdown、Syntax 等可组合组件 | 作为 Python 终端渲染基础，不自建 ANSI 转义系统 |
| prompt_toolkit | 交互式输入、补全、历史、键盘事件和多行输入 | 继续作为 Chat 输入层，不用 Rich 替代输入编辑器 |

参考资料：

- Codex CLI customization：https://learn.chatgpt.com/docs/cli-customization
- Codex CLI advanced configuration：https://learn.chatgpt.com/docs/config-file/config-advanced
- Gemini CLI terminal rendering design：https://developers.googleblog.com/en/making-the-terminal-beautiful-one-pixel-at-a-time/
- Claude Code customization tips：https://support.claude.com/en/articles/14554000-claude-code-power-user-tips
- Aider in-chat commands：https://aider.chat/docs/usage/commands.html
- Rich Console API：https://rich.readthedocs.io/en/stable/console.html
- Rich Live Display：https://rich.readthedocs.io/en/stable/live.html

### 2.1 最重要的取舍：Inline-first，不默认全屏

第一版默认采用 Inline Terminal UI：

- 输出自然写入当前终端滚动历史。
- 用户可以直接选择、复制、搜索和回看执行日志。
- Run 结束后保留任务头部、事件、最终 Summary 和 Diff 摘要。
- Chat 输入仍然由 prompt_toolkit 管理。
- 动态状态只更新局部区域，不清屏重绘整个终端。

第一版不默认启用 alternate screen。全屏 TUI 可以作为后续可选模式，但不是本阶段前置条件。

原因：

- Run 是有明确开始和结束的执行任务，保留完整 scrollback 比占用整个终端更重要。
- Chat 需要稳定的输入区和历史内容，但不需要先建设复杂的全屏布局系统。
- Rich 已提供足够的 Inline Rendering 能力。
- 通过统一的 Event Presenter 和 Component Library，后续仍然可以增加 Full-screen Mode。

### 2.2 视觉原则

- 使用语义化颜色，不在业务代码中直接拼 ANSI 转义。
- 主要状态使用符号 + 文本，不只依赖颜色区分。
- 默认深色终端友好，但不设置大面积背景色，尽量兼容浅色终端。
- 不使用彩虹色、渐变动画、无意义装饰和频繁闪烁。
- 状态变化优先更新原有状态行；只有重要事件才新增永久输出。
- 任务描述、工具参数、文件路径和错误内容必须可读、可换行、可复制。
- 终端宽度不足时自动压缩布局，不能依赖固定列宽。
- 不在默认 UI 中展示模型内部 reasoning 内容；可展示“正在分析”“正在调用工具”等执行阶段状态。已有 reasoning-stream 仍由显式参数控制。

## 3. 当前代码现状与改造依据

### 3.1 已有实现

| 文件 | 当前实现 | 本阶段处理 |
|---|---|---|
| entry/cli.py | run 命令打印 Provider、Model、Repo、Features 等文本；创建 RunEventRenderer；stream callback 直接写 stdout；结束时调用 _print_run_result | 替换为 RunTerminalUI，统一 Header、Event Stream 和 Final Summary |
| entry/chat.py | ChatSession 使用 RunEventRenderer；stream/thought callback 直接写 stdout；使用 prompt_toolkit 处理输入 | 使用 ChatTerminalUI 和统一 Console；输入编辑器继续保留 |
| entry/event_renderer.py | RunEventRenderer 将 Event 转成 click.echo 文本 | 迁移为 Event Presenter 或保留兼容 Facade |
| entry/chat_input.py | PromptSession、ChatCompleter，支持 slash command 和 @path 补全 | 保留并扩展命令、快捷键和编辑器集成 |
| agent/event_log.py | EventLog.on_append 观察事件；JSONL append 后调用 observer；observer 异常不会改变 Agent 执行 | 继续作为事件源，UI 只注册 observer |
| agent/runner.py | ExecutionRunner.run 接受 on_event 回调，CLI/Chat 等入口共享 | 不让 Runner 依赖 Rich；继续传递普通 Event |
| agent/task.py | EventType、Action、Observation、RunResult | 不改变领域事件的语义和序列化格式 |
| pyproject.toml | 已包含 rich、click、prompt_toolkit | 第一版不增加 UI 依赖 |

当前的核心问题：

1. RunEventRenderer 主要输出 [Step n] action=... 和 Observation [tool]: success，信息密度高但缺少产品层次。
2. Run 的 Header 和最终 Summary 分别由 cli.py 中的 click.echo 拼接，布局不统一。
3. Chat 和 Run 各自定义颜色 helper，输出风格容易分叉。
4. Chat/Run 的 stream callback 直接 sys.stdout.write，Event Renderer 又通过 click.echo 输出，缺少统一输出协调。
5. LLM_CALL_STARTED/FINISHED/RETRY、TOOL_EXECUTION_STARTED/FINISHED、PLAN_STEP_STARTED/COMPLETED、WORKERS_SUMMARY、CONTEXT_COMPACTED 等事件尚未形成完整的 UI 视图。
6. 成功工具输出和失败工具输出缺少统一折叠、截断和详情展开策略。
7. 最终 RunResult 只输出简单状态、Steps、Tokens、Time、Worktree 等字段，缺少结构化的 Summary、验收和变更展示。

### 3.2 必须保持的执行边界

- Agent Core 仍只产生 Event、Action、Observation、RunResult 和 Stream Callback。
- EventLog 继续负责 Trace v2 JSONL、持久化、redaction 和 observer。
- ExecutionRunner 继续负责统一执行流程。
- PermissionManager、ToolExecutor、terminal_confirm 的行为不能被 UI 改变。
- UI 不能通过解析终端字符串来推断任务状态，必须消费 EventType 和 RunResult。
- UI 不能在 Event 回调里执行 Git、测试或其他工具。
- UI 发生异常不能导致 Agent 任务失败；EventLog 当前已有 observer exception isolation，Stream Callback 也需要入口层 fail-soft 包装。
- 不修改 EventLog JSONL 的已有字段和语义来适配显示。

## 4. 目标视觉方案

### 4.1 Run：任务执行界面

Run 是本阶段优先完成的模式。一次 run 至少包含 Header、Live Status、Event Stream 和 Final Summary 四部分。

启动时：

~~~text
╭─ FORGE AGENT ───────────────────────────────────────────────────────╮
│ RUNNING   Fix the failing authentication tests                     │
│ Repo      ~/workspace/my-project        Branch dev                  │
│ Model     openai / gpt-6                Protocol responses           │
│ Mode      approval=auto  sandbox=docker  planning=always             │
╰────────────────────────────────────────────────────────────────────╯

◌ Initializing execution...
~~~

运行中：

~~~text
◌ Step 03/40 · Calling model · 12.4s elapsed
✓ Plan v1 · Fix authentication regression and verify affected tests
✓ Step 1 · Inspect authentication implementation
▶ shell · pytest -q tests/auth
  Running...
✓ shell · pytest -q tests/auth · 12 passed · 1.8s

◌ Step 04/40 · Calling model · 18.2s elapsed
▶ file_edit · src/auth/session.py
✓ file_edit · src/auth/session.py · updated

! Verification rejected · FINAL_STATE_UNVERIFIED
↻ Recovery · rerun_test · Fresh verification required
▶ shell · pytest -q tests/auth
✓ shell · pytest -q tests/auth · 14 passed · 2.1s
~~~

结束时：

~~~text
╭─ RUN SUMMARY ───────────────────────────────────────────────────────╮
│ ✓ SUCCESS                                                           │
│                                                                      │
│ Task       Fix the failing authentication tests                     │
│ Repository ~/workspace/my-project                                    │
│ Duration   32.6s       Steps  8       LLM calls  5                   │
│ Tokens     18,420      Input 15,200   Output 3,220                   │
│                                                                      │
│ Verification  PASSED                                                 │
│ Delivery      NOT REQUESTED                                          │
│                                                                      │
│ Changes                                                               │
│   M src/auth/session.py                                              │
│   M tests/auth/test_session.py                                       │
│                                                                      │
│ Summary                                                              │
│ Fixed session validation and added regression coverage.              │
╰──────────────────────────────────────────────────────────────────────╯
~~~

以上是布局示意，字段根据真实 RunResult、Acceptance、Delivery 和 Event 数据显示；没有数据时不伪造、不显示空字段。

### 4.2 Run Header

Header 展示：

- 当前状态：INITIALIZING / RUNNING / WAITING_APPROVAL / COMPLETED / FAILED / CANCELED / INCOMPLETE。
- Task 描述：最多两行，超出折叠。
- Repo 路径。
- Git Branch（可获取时）。
- Provider / Model / Protocol。
- Approval Mode。
- Sandbox / Isolate。
- Planning / Skills / MCP / Workers 等启用功能。

Header 在 Run 开始时渲染一次。除状态变化外不持续刷新整个 Header。

非 TTY 模式不显示 Panel，改为普通文本字段。

### 4.3 Live Status

Live Status 是一条或少量状态行，用于展示当前运行阶段：

- Initializing。
- Preparing Context。
- Waiting for Model。
- Model Streaming。
- Executing Tool。
- Waiting for Permission。
- Running Tests。
- Planning / Recovery。
- Acceptance。
- Delivery。

状态行可以使用 Rich Status/Live 实现，但只更新局部状态，不重绘整个历史输出。

状态变化时保留重要事件记录。例如 Tool 开始时可以显示当前状态，Tool 完成后新增一条永久结果行。

不要为每个 Token、每个小状态变化都打印新行。

### 4.4 Event Stream

Event Stream 是用户可以回看和复制的永久事件流。

推荐事件样式：

| 事件 | 符号/状态 | 展示内容 |
|---|---|---|
| TASK_START | ● | Task 开始 |
| LLM_CALL_STARTED | ◌ | Model 请求开始；主要更新 Live Status |
| LLM_CALL_FINISHED | ✓ | 模型请求完成，可显示耗时 |
| LLM_CALL_RETRY | ↻ | Retry 次数、原因和等待时间 |
| LLM_CALL_FAILED | ! | Provider 错误类型和简短原因 |
| PLAN_CREATED | ◆ | Plan 版本和 Goal |
| PLAN_STEP_STARTED | ▶ | Step ID 和描述 |
| PLAN_STEP_COMPLETED | ✓ | Step 完成 |
| PLAN_REVISED | ↻ | 新旧 Plan Version 和原因 |
| ACTION | ▶ | Tool 名称、关键参数和 Step |
| TOOL_EXECUTION_STARTED | ◌ | Tool 开始，显示当前执行状态 |
| OBSERVATION | ✓ / ! | Tool 结果、耗时、输出摘要 |
| COMPLETION_REJECTED | ! | 拒绝原因和具体条件 |
| RECOVERY_SELECTED | ↻ | Recovery Strategy 和 Attempt |
| RECOVERY_EXHAUSTED | ✕ | 恢复预算耗尽 |
| SKILL_LOADED | ◆ | Skill 名称 |
| MCP_TOOL_DISCOVERED | · | MCP capability 统计，不逐项刷屏 |
| CONTEXT_COMPACTED | ◇ | 压缩前后 Token、Checkpoint ID |
| WORKERS_SUMMARY | ◇ | Worker 数量、成功数、失败数、Usage |
| ACCEPTANCE | ✓ / ! / – | 独立验收结果 |
| DELIVERY | ✓ / ! / – | 交付结果 |
| TASK_COMPLETE | ✓ | Agent 执行完成 |
| TASK_FAILED | ✕ | Agent 执行失败 |
| TASK_INCOMPLETE | ! | 未完成原因和资源限制 |
| RUN_TERMINATED | ! | Run 终止原因 |

Event Presenter 必须兼容当前 EventType 枚举。没有对应数据时不展示不存在的细节。

### 4.5 Tool Card

Tool 是 Run 中最频繁出现的信息，必须统一样式。

默认展示：

~~~text
▶ shell · pytest -q tests/auth
✓ shell · exit 0 · 12 passed · 1.8s
~~~

工具失败：

~~~text
▶ shell · pytest -q tests/auth
✕ shell · exit 1 · 0.9s
  ImportError: cannot import name 'SessionToken'
  tests/auth/test_session.py:18
  ... 8 more lines
~~~

文件修改：

~~~text
✓ file_edit · src/auth/session.py
  3 additions · 1 deletion
~~~

要求：

- Tool Name、关键参数、运行状态、耗时统一。
- 不默认展示所有成功工具的完整输出。
- 成功工具默认展示一到三行摘要；失败工具默认展示错误和关键输出。
- verbose 模式展示更多输出。
- 超长输出折叠，显示 omitted line count。
- 文件修改可展示路径和 Diff Stat。
- Shell 命令做宽度截断，但保留完整命令用于 Trace。
- 敏感参数遵循现有 redaction 规则，不能为了 UI 把明文凭据输出到终端。
- 不在默认模式下把所有工具输出嵌入一个巨大 Panel。

## 5. Chat：交互式对话界面

Chat 使用与 Run 相同的 Theme、Console、Tool Card、Status、Markdown 和 Diff 组件，但增加输入区域和消息角色。

示意：

~~~text
╭─ FORGE AGENT ───────────────────────────────────────────────────────╮
│ my-project · dev · gpt-6 · session a12f4c                           │
│ Context 32% · 12.4k tokens · Skills 2 · MCP 1                       │
╰────────────────────────────────────────────────────────────────────╯

╭─ YOU ───────────────────────────────────────────────────────────────╮
│ 检查当前仓库的认证模块，并修复相关测试                               │
╰────────────────────────────────────────────────────────────────────╯

◆ Plan v1
  1. Inspect authentication implementation
  2. Identify regression
  3. Implement fix
  4. Run targeted tests

▶ shell · pytest -q tests/auth
✓ shell · 12 passed · 1.8s

╭─ FORGE ─────────────────────────────────────────────────────────────╮
│ 已完成认证模块检查。发现 Session Token 校验存在一个边界条件问题，   │
│ 已修复实现并补充回归测试。                                           │
╰────────────────────────────────────────────────────────────────────╯

──────────────────────────────────────────────────────────────────────
gpt-6 · my-project · 12.4k tokens · /help for commands
> _
~~~

### 5.1 输入区域

继续使用现有：

- prompt_toolkit.PromptSession。
- ChatCompleter。
- Slash Command Completion。
- @path Repository Completion。

输入区域要求：

- 稳定保持在当前输入位置。
- 支持多行粘贴和长文本。
- Enter 提交，Shift+Enter 或 Meta+Enter 插入换行（按终端能力支持）。
- 上下方向键浏览输入历史。
- Ctrl+C 中断当前输入或当前执行，遵循现有 Cancel 语义。
- Ctrl+D 退出或按现有 EOF 语义处理。
- Ctrl+G 使用 VISUAL/EDITOR 打开外部编辑器，保存后返回输入框。
- 不支持某些快捷键的终端仍然可以通过普通 Enter、命令和编辑器完成操作。

### 5.2 消息和 Markdown

- User Message 使用独立标题和轻量边框。
- Assistant Message 使用 Markdown 渲染。
- Code Fence 使用 Syntax Highlighting。
- Diff 使用独立颜色：新增行、删除行、上下文行。
- URL、文件路径和行号保持可复制。
- 长输出按段落渲染，避免一个巨大的终端 Panel。
- 流式输出期间更新当前 Assistant Message 区域，完成后固定为普通历史内容。
- 不重复输出同一段 Assistant 内容。

### 5.3 Tool 和状态

Chat 中 Tool Card 与 Run 使用同一实现。

Chat 的状态栏可展示：

- Model / Provider。
- Repository / Branch。
- Session ID。
- Context Usage。
- Token Usage。
- 当前执行状态。
- 当前启用的 Skills / MCP 数量。

状态栏信息可以通过配置关闭，不强制占用终端高度。

### 5.4 Slash Commands

保留现有命令：

- /exit
- /stats
- /session
- /new
- /resume
- /rename
- /clear
- /help
- /model

增加或完善：

| 命令 | 功能 |
|---|---|
| /status | 当前 Model、Repo、Branch、Approval、Sandbox、Session 状态 |
| /diff | 展示当前会话或本轮产生的 Git Diff |
| /tokens | Token Usage、Context Usage、Cache Usage |
| /plan | 查看当前结构化 Plan |
| /recovery | 查看最近的 Recovery 事件 |
| /skills | 查看本次 Chat 可用和已加载 Skill |
| /mcp | 查看本次 Chat 已注册的 MCP Server/Tool |
| /compact | 显示当前 Context Compaction 状态；不强制触发压缩 |

已有 /model、/clear、/resume 等命令的实际语义保持不变。命令未实现时不能只显示成功提示。

## 6. 统一 UI 组件与数据模型

### 6.1 Theme

建立语义化 Theme，不让业务模块直接传 ANSI 颜色代码。

建议 Style Token：

| Token | 用途 |
|---|---|
| app.title | Forge 标题 |
| text.primary | 正文 |
| text.secondary | 辅助说明 |
| text.muted | 次要信息 |
| status.running | 运行中 |
| status.success | 成功 |
| status.warning | 警告、待确认 |
| status.error | 失败 |
| action.tool | 工具调用 |
| action.plan | Planning |
| action.recovery | Recovery |
| diff.added | Diff 新增 |
| diff.removed | Diff 删除 |
| border.default | 普通边框 |
| border.focused | 当前输入/焦点 |

默认主题使用 Rich Theme，颜色选择遵循终端自动检测。必须支持 NO_COLOR、TERM=dumb 和无颜色模式。

### 6.2 UI Event View Model

将 Agent Event 映射为 UI 专用只读视图，不修改原始 Event：

    TerminalEvent:
        kind
        title
        detail
        status
        step
        tool_name
        tool_summary
        output_preview
        duration_ms
        timestamp
        importance
        collapsed

Presenter 根据 EventType 和 payload 构造 TerminalEvent。Renderer 只负责显示 TerminalEvent。

不要让 UI 直接依赖完整 Agent 内部状态对象，也不要把显示文本反向写入 EventLog。

### 6.3 RunSummary View Model

最终结果由 RunResult、EventLog Summary、Acceptance 和 Delivery 数据构造：

    RunSummary:
        status
        task_description
        repo_path
        branch
        model
        provider
        protocol
        elapsed_seconds
        steps_taken
        llm_calls
        total_tokens
        input_tokens
        cached_tokens
        output_tokens
        tool_call_count
        successful_tool_count
        failed_tool_count
        changed_files
        diff_stat
        acceptance_status
        delivery_status
        worktree
        summary
        error
        warnings

字段没有数据时省略，不用 0 或空字符串伪装未知状态。

Agent SUCCESS、Acceptance PASSED、Delivery DELIVERED 是三个不同结果，必须分别显示。

## 7. 建议的源码结构

新增 UI 包：

    entry/ui/
        __init__.py
        console.py
        theme.py
        capabilities.py
        models.py
        event_presenter.py
        components.py
        run.py
        chat.py
        summary.py
        streaming.py

建议职责：

| 文件 | 职责 |
|---|---|
| console.py | Console 工厂、输出通道和 Rich Console 生命周期 |
| theme.py | 语义化颜色、默认 Theme、NO_COLOR |
| capabilities.py | TTY、终端宽高、CI、颜色、Unicode 能力检测 |
| models.py | TerminalEvent、RunSummary 等 UI 数据结构 |
| event_presenter.py | EventType/payload 到 UI View Model 的映射 |
| components.py | Header、Status、Tool Card、Plan、Diff、Token Usage 等组件 |
| run.py | RunTerminalUI，管理 Run Header、Event Stream、Status 和结束状态 |
| chat.py | ChatTerminalUI，管理 User/Assistant Message、Tool Card、Status Line |
| summary.py | RunResult/Acceptance/Delivery 的最终摘要 |
| streaming.py | 统一文本流、Reasoning Stream 和 UI 输出协调 |

现有文件处理：

- entry/event_renderer.py：保留兼容入口，逐步转发到新 Event Presenter/RunTerminalUI；不要继续保留第二套格式化逻辑。
- entry/chat.py：使用 ChatTerminalUI，删除重复的颜色 helper 和直接写 stdout 的显示代码。
- entry/cli.py：使用 RunTerminalUI，收口启动 Header、Stream Callback、Event Observer 和 _print_run_result。
- entry/chat_input.py：继续负责输入、Completion 和命令补全，不承担 Rich 渲染。
- agent/runner.py：保持 UI 无关，只继续接收 on_event。
- agent/event_log.py：保持 UI 无关，只继续调用 observer。

第一版可以根据实际代码结构调整文件数量，但必须保留以上职责边界。

## 8. 流式输出、Live Rendering 与终端兼容

### 8.1 统一输出通道

当前 Chat 和 Run 的 Stream Callback 直接写 sys.stdout，Event Renderer 又调用 click.echo。第一版必须收口为一个 UI 输出协调器。

统一入口：

    ui.on_event(event)
    ui.on_stream_text(text)
    ui.on_reasoning_text(text)
    ui.on_status(status)
    ui.finish(run_result)

要求：

- Rich Console 是终端文本的统一写入入口。
- Chat Input 由 prompt_toolkit 管理。
- Stream Callback 不再直接 sys.stdout.write。
- Live Status、Event Stream、Markdown 输出和 Permission Prompt 不允许互相覆盖。
- 如果存在后台线程输出，必须使用线程安全的 UI Event Queue 或 prompt_toolkit 对应的 stdout 协调能力。
- Event Observer 异常和 UI 渲染异常不能改变 Agent RunResult。
- 关闭 UI 时恢复光标、颜色和终端状态。

### 8.2 Rich Live 使用边界

Rich Live 只用于局部动态内容：

- 当前执行状态。
- 当前工具状态。
- 短时间的模型等待状态。

不允许每个 Token 都触发整个终端历史重绘。

建议：

- Live refresh 频率 4–8 FPS。
- 只更新当前 Status 或当前 Assistant Message。
- 重要状态变化通过 Console 永久打印到 scrollback。
- Run 最终 Summary 永久保留。
- Chat 输入等待期间停止可能覆盖输入区域的 Live 刷新。
- Permission Confirmation 开始前暂停动态区域，完成后恢复。
- 终端 resize 后重新计算宽度并重绘当前动态区域。

### 8.3 Stream Rendering

最终回答流：

- 默认逐步显示模型最终文本。
- 使用 Rich Text/Markdown 组件管理输出样式。
- 流式期间不把半截 Markdown 误当成完整代码块。
- 完成后整理为可读的 Assistant Message。
- 同一段文本不能在流式阶段和完成阶段重复打印。

Reasoning Stream：

- 默认不展示模型内部 reasoning。
- 现有 --reasoning-stream 是显式 opt-in；开启后使用独立的 dim 样式。
- Reasoning Stream 与最终回答必须视觉区分。
- 不允许 reasoning callback 与 tool event 输出交错到同一行。

### 8.4 TTY / CI 降级

| 环境 | 行为 |
|---|---|
| stdout/stderr 均为 TTY | Rich Inline UI |
| stdout 重定向 | Plain Text，无 ANSI、无 Live |
| stdin 非 TTY | Chat 不启动交互 PromptSession |
| CI=true | 默认 Plain/Compact UI |
| TERM=dumb | 禁用颜色、Live 和动态光标 |
| NO_COLOR 存在 | 禁用颜色 |
| Windows Terminal / VS Code Terminal | 依赖 Rich 能力检测，不自行假设 ANSI 能力 |
| SSH | 使用终端实际检测结果；窄终端自动压缩 |
| pytest CliRunner | Plain Output，保持测试稳定 |

Plain Mode 仍然展示必要的 Task、Action、Observation、Acceptance、Delivery 和 Final Summary，只是不用 Panel、Live 和颜色。

## 9. 配置与 CLI 参数

### 9.1 配置

在 config/schema.py 增加 UIConfig：

    UIConfig:
        mode: "auto"
        theme: "default"
        show_status_line: true
        show_model_metadata: true
        show_token_usage: true
        tool_output_mode: "on_error"
        tool_output_preview_lines: 4
        live_refresh_per_second: 6
        markdown_enabled: true
        animations: false

AppConfig 增加 ui 字段。

配置校验：

- mode：auto / rich / plain。
- theme：default / light / dark / ansi；未识别主题应报配置错误或回退并给出诊断。
- tool_output_mode：all / on_error / summary / none。
- tool_output_preview_lines：正整数。
- live_refresh_per_second：1–10。
- 所有 Boolean 配置必须严格校验。
- 默认不开启装饰动画。

如果当前配置解析方式不适合引入完整 Theme 配置，可先实现最小 UIConfig，再扩展主题选择。

### 9.2 Run 参数

建议增加：

    --ui auto|rich|plain
    --verbose / -v
    --no-progress

行为：

- auto：TTY 使用 Rich，其他环境 Plain。
- rich：用户显式要求 Rich，但终端能力不满足时给出警告并安全降级。
- plain：关闭 Rich Live、Panel 和颜色。
- verbose：展示完整工具输出和详细执行事件。
- no-progress：关闭动态 Status，但保留重要事件和最终 Summary。

保留现有 --stream/--no-stream、--reasoning-stream/--no-reasoning-stream 语义。--no-stream 只关闭模型文本流，不关闭 Run 进度事件。

不允许为 UI 重构改变 approval-mode、sandbox、isolate、result-policy、planning、skills、MCP 和模型请求行为。

### 9.3 Chat 命令

Chat 继续使用现有 /help、/model、/stats 等命令。增加的命令必须真实接入功能，不能只显示成功提示。

主题和 UI 模式可以通过配置或后续 /theme、/ui 命令切换。第一版不要求实现复杂主题选择器。

## 10. 最终 Run Summary 与 Diff

Run 完成后必须展示结构化 Summary，不再只打印 Status/Steps/Tokens/Time。

推荐顺序：

1. 最终状态。
2. Task / Repo / Model。
3. Elapsed / Steps / LLM Calls / Tokens。
4. Tool Call 成功与失败统计。
5. Acceptance Status。
6. Delivery Status。
7. Changed Files / Diff Stat。
8. Agent Summary。
9. Error / Warning。
10. Worktree Branch / Path（存在时）。

规则：

- SUCCESS 使用明确成功状态。
- FAILED 显示错误原因。
- INCOMPLETE 显示未完成原因、资源限制或 max steps。
- CANCELED 显示取消状态，不伪装为失败。
- GAVE_UP 显示 Agent 主动放弃。
- Acceptance 和 Delivery 独立展示。
- 有 Patch 时默认展示 changed file list 和 diff stat。
- 完整 Git Diff 不默认全部打印；后续可提供 --show-diff 或 /diff。
- Worktree 被移除时展示真实结果策略，不显示失效路径。
- Summary 中的 Token Usage 使用 SessionUsage/RunResult 实际统计；未知用量不显示为 0。

## 11. 测试与验收

### 11.1 组件测试

新增 tests/test_terminal_ui_components.py：

- Theme 和 Style Token。
- Header 在宽度 40/60/80/120 下渲染。
- Tool Card 成功、失败、超长输出。
- Markdown、Code、Diff Rendering。
- RunSummary 缺失字段省略。
- Acceptance/Delivery 独立状态。
- NO_COLOR 和 TERM=dumb。

### 11.2 Event Presenter 测试

新增 tests/test_terminal_event_presenter.py：

- 当前 EventType 到 UI View Model 的完整映射。
- Plan / Recovery / Skill / MCP / Compaction / Worker / Acceptance / Delivery。
- LLM retry、failure、tool lifecycle。
- 未知 EventType 不崩溃。
- 敏感参数不会通过 UI 输出。
- 同一 Event 不重复渲染。

### 11.3 Run UI 集成测试

新增 tests/test_run_terminal_ui.py：

- 普通 Run。
- Isolate Run。
- Docker Sandbox Run。
- Tool 成功、失败、超时。
- Planning 和 Recovery。
- 多 Worker。
- Acceptance Passed / Failed / Skipped。
- Delivery Delivered / Failed / Not Requested。
- SUCCESS / FAILED / INCOMPLETE / CANCELED / GAVE_UP。
- --stream 和 --no-stream。
- --verbose 和 --no-progress。
- UI observer 抛异常时 Agent 结果不变。
- 最终 Summary 字段和真实 RunResult 一致。

### 11.4 Chat UI 集成测试

新增 tests/test_chat_terminal_ui.py：

- PromptSession 继续支持现有命令补全。
- @path 补全不受 Rich 渲染影响。
- 多轮 Chat Message 顺序正确。
- Stream Callback 与 Event Observer 不重复。
- Tool Card 与 Run 使用同一格式。
- /diff、/status、/tokens 等命令真实可用。
- /clear、/new、/resume、/model 保持原有语义。
- Permission Confirmation 不破坏输入区域。
- 终端 resize 不残留旧内容。

### 11.5 输出兼容测试

- Click CliRunner 下没有 ANSI 控制字符。
- 非 TTY 输出可重定向到文件。
- JSONL Trace 不受 UI 影响。
- stdout 和 stderr 的职责清楚。
- Rich Live 结束后光标恢复。
- UI 不吞掉异常或 Agent 的最终状态。
- 终端高度不足时不抛异常。
- Windows / Linux 的测试不依赖固定终端能力。

### 11.6 最终验收

以下全部满足才算完成：

1. Run 和 Chat 使用同一套 Theme 和组件。
2. Run Header、Live Status、Event Stream 和 Final Summary 完整。
3. Chat Message、Tool Card、Status Line 和 Prompt Input 完整。
4. Stream Callback 不再在业务入口中直接写 stdout。
5. Run 的 isolate/non-isolate 使用相同 UI。
6. Tool 输出默认折叠，失败信息可读。
7. Markdown、Code 和 Diff 有正确格式。
8. Acceptance、Delivery 和 Agent Status 分开显示。
9. TTY、非 TTY、CI、NO_COLOR、TERM=dumb 均可运行。
10. Live 和 Permission Prompt 不互相覆盖。
11. UI 故障不会影响 Agent 执行。
12. 原有 Chat、Run、Event Renderer 和 CliRunner 测试全部通过。
13. README、USAGE 和 docs/design 索引更新。

## 12. 实施顺序

Codex 可以一次完成全部工作，但必须遵循以下依赖顺序，避免同时修改所有入口后难以定位问题。

| 阶段 | 内容 | 交付 |
|---|---|---|
| UI-0 | 核对当前 dev 源码、现有 EventType、Chat 输入和输出回调；确定 UI API | 影响分析和接口冻结 |
| UI-1 | Theme、ConsoleFactory、TerminalCapabilities、基础 Components | 统一渲染基础 |
| UI-2 | Event Presenter、RunTerminalUI、Run Header、Tool Card、Final Summary | Run 产品化 |
| UI-3 | ChatTerminalUI、PromptSession 协调、Markdown、Status Line、Slash Commands | Chat 产品化 |
| UI-4 | Stream/Live 协调、TTY/CI 降级、Diff、Token Usage、终端 resize | 完整终端体验 |
| UI-5 | 全部测试、README/USAGE、源码索引和 architecture-map | 完整验收 |

Codex 不应只新增几个 Rich Panel 就结束。需要真正把 entry/cli.py 和 entry/chat.py 的现有输出路径接入统一 UI。

## 13. 主要源码落点

当前已存在：

- entry/cli.py
- entry/chat.py
- entry/chat_input.py
- entry/event_renderer.py
- agent/event_log.py
- agent/runner.py
- agent/task.py
- pyproject.toml
- tests/test_event_renderer.py
- tests/test_chat.py
- tests/test_chat_input_model.py

计划新增：

- entry/ui/*
- tests/test_terminal_ui_components.py
- tests/test_terminal_event_presenter.py
- tests/test_run_terminal_ui.py
- tests/test_chat_terminal_ui.py

重点修改：

- entry/cli.py：Run UI 接线、Header、Stream Callback、最终 Summary。
- entry/chat.py：Chat UI 接线、Message Rendering、Stream Callback。
- entry/chat_input.py：保持输入能力，必要时补充外部编辑器和快捷键。
- entry/event_renderer.py：迁移到统一 Event Presenter。
- docs/README.md、USAGE.md、docs/design/源码索引.md、docs/design/architecture-map.json。

Codex 实施前必须重新读取 dev 最新 HEAD 和这些文件的当前实现。具体类名和函数签名可以按实际源码调整，但职责边界和最终体验必须满足本文。

## 14. 非目标

第一版不做：

- 完整 Textual 全屏应用。
- 多窗口、分屏和终端 Tab 管理。
- 鼠标驱动的复杂 Dashboard。
- 复杂动画、渐变、ASCII Logo 动画。
- 在 UI 层重新实现 Agent 状态机。
- 修改 Tool Permission、Approval、Cancel 和 Execution Constraints。
- 修改 Trace JSONL Schema。
- 把所有工具输出完整打印到终端。
- 把 UI 展示文本作为 Agent Context。
- 把模型 reasoning 默认显示给用户。

后续可以在统一组件和 Presenter 之上增加 Full-screen Chat、Alternate Screen、主题选择器、快捷键配置和可折叠历史事件，但不能让这些扩展阻塞第一版 Inline UI。

## 15. 参考资料与源码核对

本设计的产品参考以公开文档为依据，具体交互应以对应产品当前版本为准：

1. OpenAI Codex CLI：主题、Markdown/Diff 高亮、Prompt Editor、alternate screen 与 TUI 配置。
2. Google Gemini CLI：稳定输入区、Sticky Header、窗口 resize 和 scrollback 保留。
3. Anthropic Claude Code：Status Line、模型/目录/上下文信息、颜色和快捷键配置。
4. Aider：/diff、/tokens、/map、/model、/editor 等命令式工作流。
5. Rich：Console 自动检测、Live 局部刷新、Panel、Table、Markdown、Syntax。
6. prompt_toolkit：继续承担交互输入、补全和历史，不由 Rich 取代。

Forge 的实现基线以当前 dev 源码为准：entry/event_renderer.py、entry/cli.py、entry/chat.py、entry/chat_input.py、agent/event_log.py、agent/runner.py 和 agent/task.py。
