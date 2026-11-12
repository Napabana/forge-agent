# 阶段 03：Tool System 与执行治理横向审计

> 审计日期：2026-11-12。分支：dev。当前 HEAD：e672dfd00de855d6666f0a13ca5f54eb087adbd6。
> 生产源码快照：3e3fc48814aa15604c8a3a8a4432563854f8b6c1；从该源码快照到当前 HEAD 的差异仅为 .gitignore 与研究文档，没有生产功能代码变更。
> 外部对照固定：Pi b729499585bd7dacf6e9bc52994c6f157e35f9c9；Codex 1f1f6ebb6c156d5ab1d4745155f340b897b24f02；Claude Code 使用 2026-11-12 官方公开文档。
> 证据边界：本轮读取生产源码、相关设计文档和指定测试模块；**没有运行 Forge pytest，没有执行任何注入命令，没有调用真实 LLM/MCP 服务**。对命令注入的判断来自完整静态数据流和离线命令字符串重建；没有在 Shell 中执行 payload。另做了一个独立 Python 标准库临时目录 symlink 行为检查，不属于 Forge 回归测试。

## 0. 执行摘要

阶段 03 对应阶段 0 的 C01–C14，全部进入审查。结论：

1. **发现一个 P0 级命令注入阻断项**：Git 工具和 pytest 专用工具把模型参数拼成命令字符串，最后通过 LocalRuntime 的 shell=True 执行。普通 run 注册表虽然移除了 git_add/git_commit，仍暴露 git_diff 和 test；git_diff 也被严格 read_only 白名单允许。攻击者不需要调用 shell 工具，就可能通过合法类型的 tool 参数触发额外 Shell 命令。DockerRuntime 也通过 bash -c 解释该字符串；沙箱只能限制影响范围，不能修复命令构造本身。
2. **工作区隔离并未覆盖所有工具**：FileRead/Edit/Write 等文件工具会 resolve 最终路径并检查 workspace；但 Shell、Pytest、Git 工具接受模型可控 cwd，PermissionManager 不对这些 cwd 做 workspace 约束。Shell 的只读命令判断只检查命令前缀，不检查文件参数或 cwd，cat /etc/...、env、printenv 都可能被判为只读。
3. **SearchText / FindSymbol 存在子文件符号链接逃逸**：搜索根目录经过 resolve，但遍历到的每个子文件没有再 resolve 并检查是否仍位于 workspace。Path.is_file() 和 read_text() 会跟随文件符号链接。
4. **ToolEffect 与 Permission 的职责存在实现/文档差异**：ToolEffect 主要参与 Planning 的 repository-mutation gate；PermissionManager 对原生工具只特别处理 shell、路径文件工具和 MCP，其他原生工具直接 ALLOW。因此 MAY_MUTATE_REPOSITORY 并不等价于“需要用户确认”。Chat/API 等入口可注册 git_add/git_commit，但默认 PermissionManager 不会因其 ToolEffect 自动发起 CONFIRM。
5. **MCP 接入整体分层较好**：官方 SDK、专用 asyncio loop、server namespace、schema/description/output 上限、默认不信任 read-only annotation、ToolExecutor 统一调用路径都值得保留。但存在 annotation 冲突、超时后副作用不确定、多个 server 顺序启动超时计算偏小、每 server 上限在完整拉取之后才检查、Docker 沙箱不覆盖 MCP host process 等边界。
6. **Trace 记录了调用生命周期，但还不是完整可复现实验的 Tool Manifest 记录**：native BaseTool 默认 metadata 为空；MCP_SERVER_CAPABILITIES 只记 server snapshot/tool_count；MCP_TOOL_DISCOVERED 虽在 EventType 中定义，本轮没有找到生产发射点；Trace 没有发现 per-run 完整工具 schema/hash。REPOSITORY_EFFECT 只有仓库状态变化布尔值，不记录每次工具实际修改的路径，也无法覆盖仓库外副作用。

**当前建议**：P0 修复前，不要让当前 LocalRuntime 默认配置处理不可信仓库、包含恶意 README/测试文件的任务或公开 PR benchmark。阶段 03 只形成审计证据，不在本轮修改功能代码。P0 应单独拆成安全修复任务，完成 fake-runtime 回归后再恢复不可信任务实验。

## 1. Source map 与工具清单

### 1.1 核心调用路径

| 子层 | 生产源码 | 当前职责 |
|---|---|---|
| Tool contract / Registry | [tools/base.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/base.py#L26) | BaseTool、ToolResult、ToolEffect、注册、unknown tool、参数浅校验 |
| Lifecycle | [harness/executor.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/harness/executor.py#L57) | cancel → validate → PreToolUse → Permission/Constraints → confirm → invoke → tool → PostToolUse |
| Permission | [harness/permission.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/harness/permission.py#L78) | ALLOW/DENY/CONFIRM、strict read_only、shell/path/MCP policy |
| Hooks | [harness/hooks.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/harness/hooks.py#L18) | PreToolUse / PostToolUse / UserPromptSubmit / Stop 注册表 |
| File tools | [tools/file_tool.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/file_tool.py#L35) | file_read / file_view / file_edit / file_write |
| Search tools | [tools/search_tool.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/search_tool.py#L35) | search_text / find_files / find_symbol |
| Shell / Test / Git | [tools/shell_tool.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/shell_tool.py#L20)、[tools/test_tool.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/test_tool.py#L28)、[tools/git_tool.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/git_tool.py#L25) | 命令、测试、Git 操作 |
| Runtime | [tools/runtime.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/runtime.py#L110) | LocalRuntime / DockerRuntime |
| MCP | [mcp_integration/manager.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/mcp_integration/manager.py#L18)、[mcp_integration/adapter.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/mcp_integration/adapter.py#L18) | 连接、发现、adapter、远端调用 |
| Run composition | [agent/runner.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/agent/runner.py#L1)、[agent/orchestrate.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/agent/orchestrate.py#L175) | direct / isolate、worktree、runtime、MCP manager、permission |

### 1.2 原生工具数量与入口差异

当前基础 registry 有 11 个工具；allow_git_mutation=True 时增加 git_add、git_commit，成为 13 个。

| 原生工具 | ToolEffect / 调用性质 | 说明 |
|---|---|---|
| shell | 默认 MAY_MUTATE；按命令字符串动态分类 | 只读分类不检查路径参数和 cwd |
| file_read | READ_ONLY | resolve 最终路径，workspace 内读取 |
| file_view | READ_ONLY | resolve 最终路径，按窗口返回 |
| file_edit | MAY_MUTATE_REPOSITORY | 唯一 old_text 替换，直接写回 |
| file_write | MAY_MUTATE_REPOSITORY | 全量覆盖，自动创建父目录 |
| search_text | READ_ONLY | 正则搜索；子文件 symlink 未逐个做 workspace 检查 |
| find_files | READ_ONLY | 按文件名递归查找 |
| find_symbol | READ_ONLY | 读取 Python 文件并查找 def/class；子文件 symlink 同样有问题 |
| test | READ_ONLY | 实际运行 pytest 项目代码；不代表无进程/外部副作用 |
| git_status | READ_ONLY | cwd 可由模型覆盖 |
| git_diff | READ_ONLY | path/cwd 可由模型覆盖；当前还存在命令注入 |
| git_add | MAY_MUTATE_REPOSITORY | 仅部分入口注册 |
| git_commit | MAY_MUTATE_REPOSITORY | 仅部分入口注册 |

入口矩阵：

| 入口 | 原生工具数量 | 重要差异 |
|---|---:|---|
| CLI run | 11 | 使用 _build_run_registry，移除 git_add/git_commit；仍保留 git_diff/test |
| CLI chat | 13 | _build_registry 默认允许 GitAdd/Commit；sandbox 默认关闭 |
| HTTP API | 13 | _build_registry；isolate=True，但 sandbox 默认 false；没有终端确认回调 |
| GitHub Issue | 默认 13 | create_pr=True 时移除 git_add/git_commit；当前 RunRequest 未打开 Docker sandbox |
| MCP | 动态增加 | 按 server namespace 注册，配置默认 disabled |

入口实现：[entry/cli.py registry builder](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/entry/cli.py#L85)、[CLI run sandbox default](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/entry/cli.py#L168)、[CLI chat](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/entry/cli.py#L440)、[API Runner](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/entry/api.py#L270)、[GitHub Issue Runner](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/entry/github_issue.py#L450)。

Planning / Skill 控制 schema 是 Agent 内部控制面，不属于上表 13 个原生 BaseTool；MCP tool 则注册成 BaseTool，进入 ToolExecutor。不要把“模型可见 schema 数量”直接等同于原生 ToolRegistry 数量。

## 2. C01：BaseTool、ToolResult、ToolEffect、ToolRegistry

### 当前实现

- BaseTool 默认 effect 为 MAY_MUTATE_REPOSITORY；新工具默认在 Planning 的 repository-mutation gate 上 fail-safe。
- BaseTool.is_mutating() 默认根据 effect 判断；ShellTool 按具体 command 调用动态分类。
- ToolRegistry.register() 拒绝重复 name；get_schemas() 按注册顺序返回所有工具 schema；unknown tool 返回 UNKNOWN_TOOL；未知工具在 is_mutating() 中按 mutating 处理。
- ToolRegistry 线程安全注释明确写明当前 v1 单线程，不加锁。
- BaseTool.metadata 默认空字典；ToolRegistry.get_metadata() 只返回该 metadata。

**问题 1：ToolEffect 不是通用副作用模型。** 枚举只有 READ_ONLY / MAY_MUTATE_REPOSITORY，描述的是仓库变更，而不是网络、外部系统、进程、文件系统外部路径、远端事务等所有副作用。它不能直接承担通用权限策略。

**问题 2：工具 manifest 没有在每次 run 中冻结。** ToolExecutionStarted 记录 tool_metadata；native BaseTool 默认 metadata 为空。Action 记录工具名和参数，但 EventLog 没有发现完整 parameters_schema、tool manifest hash 或版本快照。工具实现或 schema 后续变化时，仅靠 trace 不足以完全重建当时模型可见的工具接口。

**建议**：保留 BaseTool 的低耦合设计；增加 per-run tool manifest snapshot/hash，至少记录 tool name、description、parameters_schema、effect、runtime type/version 和 MCP provenance。把 ToolEffect 明确限定为 Planning 的 repository effect，不将它当成完整 permission policy。

## 3. C02：Schema 校验与 unknown tool

[tools/base.py 的校验实现](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/base.py#L42)只检查：
- params 必须是 dict；
- schema.required 中的字段必须存在；
- 对 params 中已知 property 做顶层 type 检查。

没有通用执行以下 JSON Schema 语义：
- nested object 的 properties / required；
- array.items；
- additionalProperties；
- enum / const；
- minimum / maximum / minLength / maxLength；
- pattern / format；
- oneOf / anyOf / allOf / $ref。

MCP adapter 的 validate_remote_schema 也只验证最外层 type=object、properties 是 dict、required 是 string list、JSON 可序列化以及 schema 字符数上限；不是完整 JSON Schema validator。

这不意味着所有工具都完全没有参数保护：具体 Tool 仍可能自行检查参数，Provider strict schema 也可能帮助模型输出合法参数。但 provider strict 并不是所有 OpenAI-compatible / 本地 / 文本 fallback 端点都能可靠提供的本地安全边界。

**外部对照**：Pi 固定版本的 [validateToolArguments](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/ai/src/utils/validation.ts#L1) 用 pi-schema compile JSON Schema validator，支持 formats、coercion、unresolvedRefs reject，并对 arguments 做 structuredClone 后校验；校验错误包含 JSON Pointer 路径。Forge 当前浅校验实现明显更轻，但不能作为完整 schema validation 的等价替代。

**建议**：在不破坏现有工具兼容性的前提下引入完整本地 JSON Schema validator；若短期不引入依赖，至少补齐 nested object / array.items / additionalProperties / enum / 数值边界。Schema 校验应在 permission 决策前和实际执行前保持一致。

## 4. C03：Validation → PreHook → Permission → Execute → PostHook

### 当前生命周期

[harness/executor.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/harness/executor.py#L80) 当前顺序：

1. cancel boundary：before_validation；
2. ToolRegistry.validate_tool_call；
3. 创建 ToolUseBlock；
4. PreToolUse；
5. cancel boundary：after_pre_hook；
6. ExecutionConstraints / PermissionManager；
7. decision callback 记录权限结果；
8. cancel boundary：after_permission；
9. CONFIRM callback；
10. cancel boundary：before_tool；
11. invocation_callback；
12. ToolRegistry.execute_tool（二次 schema validation）；
13. PostToolUse；
14. 返回 ToolResult，之后由 Agent 写 Observation / repository effect / Trace。

优点：
- PreToolUse block 不进入 Permission 和 Tool；
- PreToolUse 异常转为 HOOK_FAILED Observation；
- Permission/confirm subsystem 异常作为 infrastructure error；
- PostToolUse 已发生在工具副作用之后，异常只追加 diagnostic，不覆盖真实 ToolResult；
- ToolRegistry 在 hook 之后再次验证参数，避免 hook 改写参数后绕过二次 schema 校验。

### 问题与边界

- HookEvent 声明了 UserPromptSubmit / PreToolUse / PostToolUse / Stop；在本轮检查的当前生产 entry/agent 调用路径中只发现 PreToolUse 和 PostToolUse 实际触发。UserPromptSubmit / Stop 没有找到生产调用点，不能把它们描述为已完整接入的生命周期事件。
- Hooks.trigger() 同步调用 callback，没有 timeout。长时间阻塞的 Hook 无法在 callback 内被 cancel_event 强制打断。
- ToolUseBlock 是可变 dataclass。PreToolUse 收到的 block.name 可被 callback 修改；PermissionManager.check(block) 使用修改后的 block.name，但最终 ToolRegistry.execute_tool(name, params) 使用原始 name。若某个 Hook 修改 name，权限判断身份与实际执行身份可能不一致。当前没有发现产品内置 Hook 会这样做，但 API 没有冻结这个身份字段。
- PostToolUse 的非 None 返回值在 ToolExecutor 中被忽略；它当前是观察型 Hook，不能替换结果或要求 Agent 将结果作为 blocked feedback。
- UserPromptSubmit / Stop 虽可注册，但目前没有执行入口。

**建议**：
- ToolUseBlock.name 在 PreToolUse 后必须保持与原始 name 一致；更稳妥的是将 tool identity 设为 immutable，把可变参数单独复制。
- 明确 Hooks API 的输入/输出契约；若需要改写参数，使用结构化 HookResult，而不是让 callback 隐式改写 block。
- 为 Hook callback 增加超时/取消策略；策略 Hook 超时应 fail-closed，并区分 callback error 与 timeout。
- 未实现的事件要么接入产品生命周期，要么标为 reserved，避免文档暗示已实现。

## 5. C04：Permission ALLOW / CONFIRM / DENY 与 ToolEffect

[PermissionManager.check](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/harness/permission.py#L165) 的实际分支：
- strict read_only constraints 不允许的调用 → DENY；
- shell/bash → 黑名单 DENY、命令被判 repository-readonly 则 ALLOW，否则 CONFIRM；
- PATH_BOUND_TOOLS → workspace path 检查，路径通过则 ALLOW；
- MCP → 只有 adapter effect 为 READ_ONLY 才 ALLOW，其余 CONFIRM；
- 其他所有原生工具 → ALLOW。

这意味着 PermissionManager **没有根据所有原生工具的 ToolEffect 自动做确认**。ToolEffect 的 repository mutation 主要用于 Planning gate；PermissionManager 对 native Git / pytest 等工具并不统一检查 effect。

具体影响：
- CLI run 的 git_add/git_commit 被 registry 移除，所以不能通过这两个 native tool 直接提交；但 git_diff 和 test 仍然暴露。
- Chat / API 等 _build_registry 入口可以暴露 git_add/git_commit。Chat 虽传入 terminal_confirm，但只有 PermissionDecision=CONFIRM 时才会调用 callback；当前 native git_add/git_commit 会落入“其他工具 ALLOW”，不会因为 MAY_MUTATE_REPOSITORY 自动弹确认。
- file_edit/file_write 自动 ALLOW 是 Coding Agent 允许正常修改 workspace 的产品取舍，不应简单改成所有 MAY_MUTATE 都 CONFIRM。真正需要的是按 tool capability 定义细粒度策略：workspace write、Git delivery、external side effect、network、process execution 等不同权限类别。
- read_only_allows() 是另一套可信约束白名单；它不等价于 PermissionManager 的普通模式策略。

**设计文档差异**：docs/design/02-Agent与Tool生命周期.md 将 ToolEffect 描述为同时服务 Planning 和 Permission 的保守判断；当前实现中 PermissionManager 只在 MCP 分支读取 tool.effect，其他原生工具直接 ALLOW。建议更新设计文档，或者补上真正的 native tool effect policy。

## 6. C05：Workspace path boundary 与 strict read_only

### 已有正确实现

FileRead / FileView / FileEdit / FileWrite 的 _resolve_workspace_path：
- 将相对路径拼接到 workspace；
- 调用 Path.resolve()；
- 使用 commonpath 检查最终真实路径是否在 workspace 内；
- 检查 .agents/skills 内部路径。

因此文件工具的直接 symlink target escape 有额外的最终路径校验。PermissionManager 自己的 _path_escape_reason 只使用 abspath（词法路径），但实际 FileTool 又执行 resolve 后的二次检查，所以不能只看 PermissionManager 就断言 FileTool symlink 一定可逃逸。

### 已确认的缺口 A：Shell / Test / Git 的 cwd 不受 workspace 约束

entry/cli.py 的 registry builder 明确写明：default_cwd 是 shell/test/git 默认工作目录，LLM 显式传 cwd 仍可覆盖；workspace 单独用于文件/搜索工具。

PermissionManager 的 workspace 检查只覆盖 PATH_BOUND_TOOLS，不覆盖 shell、test、git_status、git_diff、git_add、git_commit。Git 和 pytest 工具直接采用 params.cwd 或 default_cwd；ShellTool 也直接采用 params.cwd 或 default_cwd。

所以 workspace 不是所有工具共享的强制边界。strict read_only 也只检查工具名和少量 shell cmd 字符串，不检查 cwd：
- shell 的 pwd / ls / ls -la 可以在 cwd=/etc 等路径执行；
- git_status / git_diff 在 strict read_only 下按工具名允许，但 cwd 可指向其他仓库；
- test 在 strict read_only 下被拒绝，但普通模式下 cwd/path 都由参数决定。

### 已确认的缺口 B：Shell 的“只读”没有路径含义

shell_tool.py 的只读前缀包含 cat、head、tail、less、find、grep、rg、env、printenv 等。_is_repository_readonly() 检查命令组合、重定向、find 的危险 action 等，但没有检查文件 operand 是否位于 workspace。

因此：
- cat /etc/passwd 可被判为只读；
- env / printenv 可读取进程环境；
- grep / find / rg 可以扫描 workspace 外路径；
- cwd 显式指向外部目录时，pwd / ls 也可能被判为只读。

ToolResult.output 直接转换成 Observation 并进入 Agent history；Trace redaction 只作用于 EventLog 持久化 payload，不会先对模型可见的 Observation 做同样的 redaction。因此 Trace 中某些 credential pattern 被遮蔽，不代表模型上下文没有看到原始输出。

### 已确认的缺口 C：Search 工具对子文件 symlink 未做真实路径检查

SearchTextTool / FindSymbolTool 的搜索根目录由 _resolve_search_path() resolve 并检查 workspace；但 _iter_files() 只对遍历出来的 filepath 做 lexical skip，再调用 filepath.is_file() 和 read_text()，没有 resolve 每个候选文件并重新检查 commonpath。

Python Path.rglob() 会返回指向文件的 symlink，Path.is_file() 会跟随 symlink，read_text() 也会读取目标文件。本轮用独立临时目录和文件 symlink 做了标准库行为验证；没有运行 Forge 测试。

这意味着仓库内一个指向 workspace 外部文件的文件 symlink，可能让 search_text / find_symbol 读取外部文件；若链接指向 .agents/skills 等内部运行时目录，lexical skip 也可能无法识别真实目标。FileReadTool 的 resolve 检查不自动保护 SearchTextTool。

**结论**：ExecutionConstraints(read_only=True) 是工具调用子集限制，不是文件系统只读沙箱；workspace 目前只对部分文件工具生效。docs/design/06-隔离与独立验收.md 中“workspace-bound Permission 防路径逃逸”的表述需要限定为文件工具，或补齐其他工具的路径约束。

## 7. C06：cooperative cancel 与工具执行边界

当前 ToolExecutor 在 validation 前、PreToolUse 后、Permission 前后、实际 Tool 前检查 cancel_event。Tool 一旦进入同步 execute()，不会强制中断；执行完成后保留真实 ToolResult、执行 PostToolUse、追加 cancel_requested_after_tool，再由 Agent 在后续安全边界停止。

这是合理的“副作用已发生就不能伪装成未发生”的设计，但有明确限制：
- Python 同步 Hook / confirm callback 执行中不能被 cancel_event 立即打断；
- LocalRuntime 的 subprocess.run(timeout=...) 超时控制的是直接子进程调用；命令派生的后台进程/子孙进程是否全部结束不能仅由当前接口保证；
- DockerRuntime 超时会终止 docker exec 客户端等待，但工具命令在容器内的生命周期仍应通过容器清理和进程组测试验证；
- MCP ToolAdapter 是同步接口，call_tool() 阻塞等待专用 asyncio loop 的 future；cancel_event 没有传入远端调用。取消发生在远端操作开始后，仍可能等到结果返回才到达 Agent 安全边界。

**MCP 特别风险**：MCP timeout 会将本地 future cancel 并返回 TIMEOUT，但不能据此证明远端服务没有执行副作用。RecoveryPolicy 对第一次 timeout 选择 RETRY 一次；若远端操作已完成但响应丢失，重试可能重复创建/部署/扣款等非幂等动作。当前 adapter 没有通用 idempotency key 或“执行结果不确定”状态。

**建议**：对 MCP timeout 区分“未发送 / 已发送但结果未知 / 明确失败”；默认不自动重试非幂等 MCP 操作。需要重试时由 server/tool 提供 idempotency key 或显式声明 idempotent。进程工具采用进程组/容器级终止并测试子进程残留。

## 8. C07：FileRead / FileView / FileEdit / FileWrite

### 优点

- 四种文件工具都使用 workspace 构造参数；生产 registry builder 显式传入 workspace，而不是从 default_cwd 隐式推导。
- FileRead / FileView / FileEdit / FileWrite 通过 _resolve_workspace_path() 检查最终 resolve 路径。
- FileEdit 要求 old_text 非空且恰好出现一次，使用 bytes 替换，保留替换区域之外的原始字节；对 CRLF/编码字节稳定性比全文件 decode/encode 更好。
- FileWrite 明确是全量覆盖，自动创建父目录，工具描述要求先读文件，语义简单。

### 代价与待改进

- FileRead / FileView 先对整个文件 read_text().splitlines()，再截取 MAX_READ_LINES / VIEW_WINDOW_LINES。输出被限制，不代表读取内存被限制；单个巨大文件仍可能占用大量内存。
- FileEdit 读完整文件、替换后直接 write_bytes；FileWrite 直接 write_text。都不是原子替换，进程中断/磁盘错误可能留下不完整文件。
- 没有发现每次读写的最大字节数统一限制；大文件/大内容的资源上界主要受模型上下文和 OS 资源间接影响。
- 通过目录 symlink 访问 workspace 内另一个文件时，最终 target 在 workspace 内，属于允许访问；通过文件 symlink 指向 workspace 外部时，resolve/commonpath 会拒绝。若需要拒绝所有 symlink，而不仅是外部 target，需要另行定义产品语义。

**建议**：先增加 max_bytes 和读取窗口化，避免为展示 100 行而把数 GB 文件完整读入内存；写操作可使用同目录临时文件 + fsync/replace，但需保留权限、编码和文件元数据策略。

## 9. C08：SearchText / FindFiles / FindSymbol

- SearchText 支持 Python 正则，最大结果数限制输出条数，不限制总扫描字节数、最大单文件大小或 regex 执行时间。
- SearchText 对每个文件执行 read_text().splitlines()，完整读取文件后再逐行 regex.search。
- FindSymbol 使用 re.escape(symbol)，避免用户 symbol 被解释为复杂正则；但同样完整读取候选 Python 文件。
- FindFiles / SearchText / FindSymbol 都通过 _iter_files() 遍历；MAX_RESULTS 限制输出，不等于限制扫描工作量。
- _iter_files() 对每个候选子文件不 resolve 并检查 workspace，产生 C05 的 symlink escape。
- SearchText 的 pattern 是任意 regex；Python re 没有内建匹配超时。恶意或极端回溯模式加长行可形成 ReDoS 风险。当前测试只覆盖非法 regex、普通匹配和大小写，没有 catastrophic-backtracking / 超大文件测试。

**建议**：
1. 每个候选文件 resolve 后检查 workspace commonpath，并检查真实目标是否属于内部 skills 路径；
2. 限制最大文件字节数、总扫描字节数、最大扫描文件数；
3. 正则采用可控子集或可中断 regex engine；至少限制 pattern 长度、输入行长度和单次搜索预算；
4. 结果数上限和扫描预算分开计量并写入 ToolResult diagnostics。

## 10. C09：Shell 命令、只读检测、超时与输出

### 当前安全措施

- 硬拦截若干危险子串，例如 rm -rf /、mkfs、dd if=、fork bomb 等；
- 只读命令采用有限前缀 allowlist；
- find 单独解析并拒绝 -delete / -exec / -ok / -fprint 等 action；
- _is_repository_readonly 支持有限的 ; / && / pipeline 组合，遇到重定向、命令替换、后台执行、|| 或未知命令时不认为只读；
- PermissionManager 对非只读 Shell 返回 CONFIRM；无 confirm callback 时 ToolExecutor 默认拒绝 CONFIRM；
- Shell 输出截断到 MAX_OUTPUT_CHARS。

### 风险

- allowlist 证明的是“命令字符串看起来只读”，不是“访问范围只在 workspace”。cat /etc/passwd、env、printenv、grep /home/user 等可能被直接 ALLOW。
- cwd 可由模型覆盖；只读判断没有 cwd 参数。
- timeout 是模型可控整数，没有上限校验；LocalRuntime 的 timeout 直接使用该值。极大 timeout 会让一次调用长期占用 Agent；同步 Tool 内部也不能及时响应 cancel。
- ShellTool standalone 的 confirm_callback=None 会跳过 ShellTool 自己的确认层；生产 Runner 依赖外层 PermissionManager。Agent 低层默认使用 raw executor，不会自动获得产品级 PermissionManager，直接使用 Agent API 的调用方必须显式注入受限 executor。
- shell 是唯一明确允许执行任意命令字符串的工具，应该与其他需要 argv 的工具严格区分，不能让 Git/Pytest 复用 shell string path。

**建议**：Shell 只保留真正需要 shell 语义的用户命令；只读工具应通过 File/Search/Git 专用接口限制 workspace。对 shell 命令的批准信息应包括规范化 cwd、完整 command、sandbox mode，而不只是 command 字符串。

## 11. C10：Pytest / Git 原生工具 —— P0 阻断项

### 11.1 命令注入静态数据流

[Git 的 _run_git()](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/git_tool.py#L25) 把 list[str] 拼成一个 Shell command：
- 只有参数包含空格时才加双引号；
- 不处理双引号、分号、命令替换、重定向等 Shell 元字符；
- 最后调用 runtime.exec(cmd, ...)。

[LocalRuntime.exec()](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tools/runtime.py#L110) 使用 subprocess.run(cmd, shell=True)。DockerRuntime 虽然在宿主端使用 argv 启动 docker exec，但容器内执行仍是 bash -c cmd，因此同样会解析该字符串。

GitDiffTool.execute() 将模型提供的 path 放在 git diff -- 之后；GitAddTool 将模型提供的 paths 追加到 git add；GitCommitTool 将模型提供的 message 作为 -m 参数。Git 的 -- 只终止 Git 自己的 option parsing，不会终止外层 Shell parsing。

离线重建命令字符串（**仅构造字符串，没有执行**）：

| 模型工具参数 | 当前构造出的命令字符串 | 结果 |
|---|---|---|
| git_diff: path=";id" | git diff --no-ext-diff --no-textconv -- ;id | Shell 将分号视为命令分隔符 |
| git_add: paths=[";id"] | git add ;id | Shell 将分号视为命令分隔符 |
| git_commit: message=";id" | git commit -m ;id | Shell 将分号视为命令分隔符 |

PytestTool.execute() 同样把 path 和 args 拼成 cmd_str；args 使用 str.split() 后追加到 command parts，再 join。args=";id" 会形成：

python -m pytest tests/ --tb=short --no-header -q ;id

这不是“模型给 pytest 一个奇怪参数”，而是运行时 shell 可能把后半段作为第二条命令执行。

### 11.2 为什么 Permission / read_only 没有挡住

- PermissionManager 对 shell、PATH_BOUND_TOOLS、MCP 之外的原生工具直接返回 ALLOW。
- git_diff 和 test 都不属于 PATH_BOUND_TOOLS，也不以 shell 名称进入 shell command classifier。
- _build_run_registry 虽然关闭 git_add/git_commit，但保留 git_diff 和 test，因此默认 CLI run 仍有触发路径。
- 更严重的是，git_diff 在 _STRICT_READ_TOOLS 中；ExecutionConstraints(read_only=True) 只按工具名允许 git_diff，不检查 path 参数是否包含 Shell 语法。因此这个路径也可能绕过 strict read_only。
- ToolRegistry 的顶层 string 类型校验不会拒绝 ;id 这样的合法字符串。
- --confirm 只在 PermissionDecision=CONFIRM 时触发；git_diff/test 的普通 PermissionDecision 是 ALLOW，不会弹确认。

### 11.3 严重度

<badge> P0 · 安全阻断项 </badge>

威胁条件：模型可调用上述 native tool；执行 runtime 是 LocalRuntime，或调用 DockerRuntime 中的 bash -c；参数可被模型或仓库内容影响。

影响：绕过 Shell 工具的确认逻辑，执行模型参数以外的 Shell 命令。默认 CLI run 仅移除 git_add/git_commit，并没有消除 git_diff/test 两条路径。默认 CLI --sandbox=false、Chat --sandbox=false、API sandbox 默认 false；在这些配置下，命令直接运行在宿主进程权限下。

本轮没有执行攻击命令，也没有声称动态 exploit 已复现；但命令构造、shell 解释器、ToolRegistry 暴露、PermissionManager ALLOW 分支均为已读取源码事实，静态执行路径完整。对不可信仓库/公开 PR benchmark，应按 blocker 处理。

### 11.4 最小修复方向

不要用 shlex.quote 继续拼接 shell string 作为主要修复；跨平台和嵌套 shell 的 quoting 很容易再次出错。

- Runtime 增加 exec_argv(argv: Sequence[str], cwd, timeout)。
- LocalRuntime 使用 subprocess.run(argv, shell=False)。
- DockerRuntime 使用 docker exec argv 形式直接传递可执行文件和参数；ShellTool 单独保留 exec_shell(command) 的明确接口。
- Git 工具统一传递 argv：git、diff、--、path 等作为独立参数。
- PytestTool 将 extra args 改成 array[string] schema，或者采用明确的参数 tokenizer，再通过 argv 执行；不能把解析结果重新拼成 Shell 字符串。
- 不要让模型提供的 cwd 越过 workspace；在 runtime 调用前对 cwd resolve/commonpath。
- 新增 fake runtime capture tests，不执行 payload，只断言命令作为 argv 传递、没有 shell=True / bash -c 的二次解释。

### 11.5 必须补的回归测试

1. git_diff path=";id"：fake runtime 只能收到 argv，不允许出现第二条命令。
2. git_add paths=[";id"]、git_commit message=";id"：同上。
3. pytest args=";id" 和 path 含 Shell 元字符：作为 pytest 参数处理，不允许 shell 解析。
4. 上述 git_diff 测试在 ExecutionConstraints(read_only=True) 下仍不得执行 Shell 元字符。
5. 默认 CLI run registry（allow_git_mutation=False）必须验证 git_diff/test 仍受到相同的参数安全保护。
6. DockerRuntime 与 LocalRuntime 使用同一 argv contract；容器内不得通过 bash -c 重新解释 native tool 参数。

## 12. C11：MCP adapter / server trust / remote effects

### 正向设计

- MCPConfig 默认 disabled；每个 server 有独立 id、transport、command/args/env 或 URL、timeout、trust_read_only_annotations。
- MCP manager 使用官方 Python MCP Client，在专用 asyncio loop thread 内管理连接；工具通过 namespaced name 注册成普通 BaseTool。
- server id 与 remote tool name 经规范化并限制长度，Registry 的 duplicate-name 检查可防止静默覆盖。
- 每 server 当前限制 128 个 tools；description 2,000 chars；schema 32,000 chars；输出 16,000 chars。
- trust_read_only_annotations 默认 false；未知/不可信 annotation 默认 MAY_MUTATE_REPOSITORY，需要 CONFIRM。
- remote isError、invalid params、timeout、remote failure、manager lifecycle error 分别映射为可观察的 ToolErrorType。
- manager 对重复 pagination cursor 有检测；close 有 join timeout，生命周期异常不会被静默忽略。

### 问题

**MCP-A：destructive_hint 与 read_only_hint 冲突时，destructive 没有优先级。**

MCPToolAdapter.effect 当前只判断：
- server config trust_read_only_annotations=True；
- annotations.read_only_hint is True。

没有同时要求 destructive_hint is not True。若 server 同时声明 read_only_hint=True 和 destructive_hint=True，adapter 仍可能被分类为 READ_ONLY，PermissionManager 因而 ALLOW。测试覆盖了 destructive_hint 单独为 true，但没有覆盖两个 hint 同时为 true。建议 destructive_hint=True 必须覆盖 read_only_hint，冲突或未知时 fail closed。

**MCP-B：远端 schema 只做外层结构校验。**

validate_remote_schema 不会完整执行 JSON Schema 语义；ToolRegistry 仍只有顶层 type 校验。MCP server 应是最终参数验证方，但 Forge 的本地校验不能声称覆盖远端 schema 全部约束。

**MCP-C：每 server 128 tools 的上限在完整分页后才检查。**

_list_all_tools() 先累积所有分页结果，之后 _supervisor 才判断 len(tool_items)>128。没有全局 server 数上限、全局 tool 数上限或最大 page 数。重复 cursor 能检测，但无限增长的不同 cursor 只能依靠 server timeout 间接限制。对受信配置一般不是高风险，但不构成强资源上限。

**MCP-D：startup_timeout 与顺序连接的总时长不一致。**

start() 的等待上限是 max(enabled_server.timeout_seconds)+5；_supervisor() 却逐个 server 顺序执行 connect 与 tools/list，每个步骤都有独立 timeout。多个 server 的累计启动时间可能大于单个最大 timeout，导致 start() 过早触发 startup timeout。应按实际总预算求和，或并发启动并设置全局 deadline。

**MCP-E：Docker sandbox 不包含 MCP server 进程。**

orchestrate_run() 在宿主 Python 进程中调用 attach_mcp_tools()；stdio server 通过 StdioServerParameters 启动，HTTP client 也由 host manager 运行。DockerRuntime 只包裹 Shell/Test/Git 等 Runtime.exec 调用。因此 sandbox=True 时，MCP server 仍处于宿主权限/网络边界；Docker --network none 不会自动限制 host MCP client/server。

**MCP-F：timeout 后远端副作用结果不确定。**

call_tool() 在 future 超时后 future.cancel()，但无法证明远端 server 在收到取消前没有完成副作用。MCPCallTimeout 映射为 TIMEOUT，RecoveryPolicy 第一次 timeout 会选择 RETRY 一次。对非幂等工具可能重复执行。应增加 idempotency contract 或“不确定结果”错误分类，不要将 timeout 等价为“工具没执行”。

**建议**：保留 manager/adapter 结构；增加 destructive 优先级、全局资源预算、startup global deadline、MCP tool idempotency metadata、host-vs-container trust boundary 文档。MCP server 配置本身是可信本地配置，不要将其视为从模型文本自动产生的 capability。

## 13. C12：Entry / Runtime / Worktree / Sandbox 的实际边界

- CLI run 的 --sandbox 默认 false；CLI chat 的 --sandbox 默认 false。
- API 的 sandbox 参数缺省 false，但 API run 使用 isolate=True；Worktree ≠ Docker sandbox。
- orchestrate_run(sandbox=False) 使用 LocalRuntime，命令 cwd 指向 host worktree。
- sandbox=True 时 DockerRuntime 使用 network=False、readonly_root=True、资源限制、worktree rw mount；容器内仍通过 bash -c 执行 Runtime.exec 的 command string。
- Docker sandbox 为 worktree 额外挂载主仓库 .git common dir；该 extra_mounts 没有 :ro 后缀，默认是 rw。readonly_root 只限制容器 root FS，不会把这个额外挂载变成只读。
- MCP manager 在 host Python 进程创建，不在 Docker container 内。

因此必须保持以下概念区分：
- Worktree：Git 结果隔离；
- Permission / ExecutionConstraints：工具调用策略；
- DockerRuntime：进程/网络/根文件系统/资源边界；
- MCP server：独立 host capability；
- Acceptance：工具执行之后的独立结果校验。

**建议**：默认 local runtime 不是不可信仓库安全环境。若要跑公开 PR 或带恶意测试的 benchmark，先修复 P0，并将 sandbox 设为可信入口的强制选项；同时审查 .git rw mount 和 host MCP server。不要在简历或文档中把 worktree 称为 sandbox。

## 14. C13：Tool Failure / Infrastructure / Timeout

当前错误分类整体清楚：
- ToolResult 区分 UNKNOWN_TOOL、INVALID_ARGUMENTS、PERMISSION_DENIED、TIMEOUT、TOOL_EXECUTION、REMOTE_CAPABILITY、INFRASTRUCTURE、HOOK_BLOCKED、HOOK_FAILED。
- ToolRegistry 捕获普通 Tool.execute exception 并转换为 TOOL_EXECUTION。
- Permission/confirm subsystem exception 在 ToolExecutor 中成为 ToolExecutorInfrastructureError。
- PreToolUse block/exception 是可恢复 Observation；PostToolUse exception 只追加 diagnostics。
- MCP manager lifecycle error 作为 infrastructure；MCP remote failure/invalid params 是工具级错误；timeout 作为独立 ObservationStatus.TIMEOUT。
- Recovery classify_tool_failure 将 infrastructure 视为 terminal fatal，将 permission_denied 映射权限失败，test 工具映射 TEST_FAILURE，其余映射 TOOL_FAILURE；timeout 首次 RETRY 一次。

问题在于 timeout 的副作用语义因工具类型不同：
- Local subprocess timeout 与 MCP remote timeout 不同；
- MCP 远端可能已经完成动作但响应丢失；
- Docker exec 客户端超时不必然等价于容器内进程已停止；
- 同步 Tool cancel 只保证 Agent 在下一安全边界停止，不保证工具进程/远端事务被撤销。

建议保留错误分类框架，增加 side_effect_state：
- not_started；
- completed；
- failed_before_effect；
- outcome_unknown。

对于 outcome_unknown，禁止无条件自动 retry；需要工具幂等声明、幂等键或人工确认。

## 15. C14：Invocation / Observation / Repository Effect / Trace

### 已有证据链

Agent 在 ToolCall 分支先记录 TOOL_EXECUTION_STARTED span，再进入 ToolExecutor。PermissionManager decision callback 写入 PERMISSION_DECISION；权限通过后 invocation_callback 写 TOOL_INVOCATION_STARTED；ToolResult 后写 TOOL_EXECUTION_FINISHED/FAILED；再计算 repository_changed / repository_content_changed 并写 REPOSITORY_EFFECT；随后生成 Observation。

EventLog 在 JSONL 写入边界调用 redact_trace_value，常见凭证字段与 Bearer / sk- / ghp_ / github_pat_ / inline secret pattern 会被遮蔽。

### 证据缺口

1. TOOL_EXECUTION_STARTED 是整个 Tool lifecycle span 的起点，发生在 validation/permission 前；TOOL_INVOCATION_STARTED 才发生在 permission 通过后。解释 trace 时必须区分 attempt span 与真正进入 Tool 的时间。
2. PERMISSION_DECISION 记录参数，但 redaction 是基于敏感 key 和常见字符串 pattern 的启发式处理，不是通用 DLP。
3. Trace redaction 只保护持久化 EventLog；原始 ToolResult.output 仍直接进入 Observation / Agent history。不能把“Trace 被 redacted”解释为“模型没有看见 secret”。
4. REPOSITORY_EFFECT 只记录 repository_changed / repository_content_changed 两个布尔值，不记录 per-tool changed_files；仓库外文件、环境变量读取、进程、网络、MCP remote side effect 不会被这两个字段完整表达。
5. BaseTool.metadata 默认空；MCP_SERVER_CAPABILITIES 记录 server snapshot/tool_count，但本轮未发现生产代码发射 MCP_TOOL_DISCOVERED；Trace 没有完整 native/MCP schema snapshot/hash。
6. EventLog 可以 replay 事件顺序，但没有完整 tool manifest/version、runtime policy snapshot、MCP server provenance config 的全量冻结时，不能单靠 JSONL 重建每次工具调用的完整执行环境。

**建议**：
- 每个 run 写 tool_manifest_hash 和 tool schema snapshot；
- MCP discovery event 记录 server id、tool name、schema hash、annotation trust、transport（不要记录 URL/env/secret）；
- REPOSITORY_EFFECT 记录可安全获取的 changed_paths，并标记 unknown_external_effect；
- 记录 sandbox/runtime policy snapshot；
- 将“Trace secret redaction”与“模型上下文数据访问控制”分别设计。

## 16. Pi / Codex / Claude Code 对照

### 16.1 Pi Agent（固定源码）

- [JSON Schema validation](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/ai/src/utils/validation.ts#L1) 使用 pi-schema compiled validator，formats/coercion/ref validation、structuredClone、错误路径。
- [ToolCall preparation](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L704) 先 prepareArguments、validateToolArguments，再调用 beforeToolCall；hook 可以 block；tool execute 接收 AbortSignal。
- [ToolCall finalization](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L821) 执行后运行 afterToolCall，可按字段替换 result/content/details/usage/isError。
- [Tool batch execution](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L485) 支持 sequential / parallel；工具可设置 executionMode=sequential。
- Pi 通用 Agent core 提供 validation 与 hook points；具体 permission/approval 策略由宿主 coding-agent/extension 集成，不把 generic loop 等同于完整 PermissionManager。

### 16.2 Codex CLI（固定源码）

- [ToolRegistry.dispatch_any_with_state](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/registry.rs#L752) 有 invocation reservation、tool kind 检查、environment resolve、PreToolUse、serving assessment permission context、NativeReview、tool start/finish 和 cancellation token。
- [ParsedExecCommand::prepare / approval_requirement](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/handlers/unified_exec/preparation.rs#L60) 把 shell command 解析到 PreparedExecCommand，再结合 approval_policy、permission_profile、environment exec policy、sandbox_permissions、prefix rules 计算 approval requirement。
- [ExecPolicy](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/exec_policy.rs#L342) 对 parsed commands 批量匹配 policy，区分 Forbidden / Prompt / PromptUser 等决策，而不是只依赖一个命令前缀白名单。
- Codex 的 exec command 本身仍允许用户请求 shell command；安全性来自解析后的审批策略、sandbox permissions 和进程执行环境组合，而不是“shell string 永远安全”。

### 16.3 Claude Code（官方公开文档，不是内部源码）

截至 2026-11-12 官方文档：
- [Permissions](https://code.claude.com/docs/en/permissions)：Manual mode 对 Bash 通常要求批准，内置只读命令除外；文件 Read 在 working directories 内可不询问；Edit/Write 默认需要批准；支持细粒度 allow/ask/deny 规则。
- [Symlink permissions](https://code.claude.com/docs/en/permissions#symlinks)：路径权限同时检查用户请求路径和最终 resolve 路径；Read/Edit/Write 还会在实际打开时确认路径仍解析到批准目标。
- [Hooks](https://code.claude.com/docs/en/hooks)：PreToolUse 可 allow/deny/ask/defer，输入包括 tool_name/tool_input/tool_use_id；Agent SDK callback hook 超时会阻止工具调用。
- [Sandboxing](https://code.claude.com/docs/en/sandboxing)：OS-enforced sandbox 只包裹 Bash/PowerShell/Monitor commands，默认关闭；文件工具、MCP servers 和 hooks 在 sandbox 外运行。sandbox 的默认 writes/read/network/env 边界有单独配置。

重要的对比结论：Claude 官方也明确区分 shell sandbox 与 file tools/MCP/hooks 的执行边界；Forge 的 DockerRuntime 同样不能自动约束 host MCP server。Claude 对 symlink 的双路径检查比 Forge Search 子文件遍历更完整。上述是公开文档行为，不推断 Claude 内部闭源实现。

## 17. 风险登记与修复顺序

| ID | 严重度 | 发现 | 影响范围 | 先后顺序 |
|---|---|---|---|---|
| T03-01 | P0 | Git/Pytest 参数拼接 Shell command + shell=True | 默认 run 可经 git_diff/test 绕过 shell confirmation；git_diff 还可绕过 strict read_only | **立即修复，benchmark blocker** |
| T03-02 | P1 | Shell readonly 分类不检查路径/cwd，cat/env/printenv 直接 ALLOW | 工作区外文件/环境变量可能进入模型上下文 | P0 后 |
| T03-03 | P1 | SearchText/FindSymbol 子文件 symlink 未 resolve+workspace check | 外部文件、内部 runtime skill 内容可能被搜索读取 | P0 后 |
| T03-04 | P1 | shell/test/git cwd 可被模型覆盖；strict read_only 不检查 cwd | 工作区外读取/执行 | P0 后 |
| T03-05 | P1 | PermissionManager 对原生工具 generic ALLOW，ToolEffect 未形成统一 native policy | GitAdd/Commit 等没有自动 CONFIRM；设计文档容易误解 | 设计策略先明确 |
| T03-06 | P1 | MCP read_only_hint 与 destructive_hint 冲突时 destructive 不优先 | trust_read_only_annotations=true 时可能错误 ALLOW | 小修 + 测试 |
| T03-07 | P1 | MCP timeout 后 outcome unknown，但 Recovery 首次 timeout retry | 非幂等远端动作可能重复 | 定义幂等契约 |
| T03-08 | P2 | ToolRegistry / MCP schema 不是完整 JSON Schema validator | nested/enum/items/additionalProperties 等约束不完整 | P0 后 |
| T03-09 | P2 | Hook callback 同步无 timeout，ToolUseBlock.name 可变；两个 Hook event 未接入 | 卡住 lifecycle / policy identity mismatch | 后续治理 |
| T03-10 | P2 | Search regex 无时间预算、文件读取无字节预算 | ReDoS / 大文件资源消耗 | 性能加固 |
| T03-11 | P2 | MCP startup max timeout 不匹配顺序连接总时长；每 server limit 后置且无全局上限 | 多 server 启动和 metadata 资源上界 | MCP 加固 |
| T03-12 | P2 | Trace 缺少 per-run schema hash、per-tool changed paths | benchmark replay / 数据归因不足 | Stage 09/11 联动 |

P0 在这里表示：模型可控参数可直接导致未经确认的任意 Shell 命令执行，并且默认本地运行时可达。该定级基于静态调用链，不代表本轮执行了 exploit。修复前不要把该分支用于不可信仓库的开放式 Agent 运行。

## 18. C01–C14 暂评分

评分维度：设计 2、实现 3、安全边界 2、测试/Trace 证据 2、扩展性 1。分数来自源码与测试文本审查，不是动态安全测试，也不是生产攻击测试。

| ID | 审计对象 | 暂分 / 10 | 置信度 | 主要原因 |
|---|---|---:|---|---|
| C01 | ToolRegistry / manifest | 7.2 | 中 | 注册简单、unknown fail-safe；入口差异和 manifest snapshot 不足 |
| C02 | Schema / arguments | 4.6 | 高 | 本地只做 required + top-level type |
| C03 | Hook lifecycle | 6.5 | 中 | 主链清晰；事件未全接入、同步无 timeout、身份可变 |
| C04 | Permission | 4.3 | 高 | shell/path/MCP 有分支，但 native effect 默认 ALLOW |
| C05 | Workspace / read_only | 3.8 | 高 | FileTool 最终路径检查好；Shell/cwd/Search symlink 缺口 |
| C06 | Cancel | 7.0 | 中 | 安全边界清晰；同步/远端副作用不可强制撤销 |
| C07 | File tools | 7.2 | 中 | resolve/path check 与精确替换好；完整读文件、非原子写 |
| C08 | Search tools | 4.8 | 高 | symlink、regex 时间预算、全文件读取 |
| C09 | Shell | 4.4 | 高 | 有黑名单/有限 allowlist，但只读分类不限定 workspace |
| C10 | Pytest / Git | 2.0 | 高 | P0 command injection 静态路径 |
| C11 | MCP | 6.4 | 中 | 架构分层好；annotation、timeout、host trust boundary |
| C12 | Entry / Runtime | 5.5 | 中 | Worktree/Docker 可组合；sandbox 默认关闭且 MCP 在 host |
| C13 | Error / Recovery | 7.4 | 中 | typed error 与恢复清晰；timeout side-effect ambiguity |
| C14 | Trace / Evidence | 6.9 | 中 | 生命周期可追踪；缺 schema manifest 和逐工具 changed paths |

不计算 14 项平均分作为 Forge 总评分；当前有 P0 阻断项，平均分会掩盖风险。

## 19. 测试覆盖与新增离线测试矩阵

### 已检查的现有测试

- [tests/test_tool_lifecycle_p0_2.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_tool_lifecycle_p0_2.py)：validation / pre-hook / permission / confirm / cancel / post-hook / infrastructure 语义；重点测试使用 RecordingTool / shell 例子。
- [tests/test_execution_constraints.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_execution_constraints.py)：strict read_only 拒绝写工具和部分 Shell 表达式，允许 file_read/file_view/git_status/git_diff/search/find/shell 固定查询。
- [tests/test_day3.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_day3.py)：Shell blacklist、SearchText / FindFiles / FindSymbol / Pytest / Git 基础行为；pytest extra args 只检查普通 -v。
- [tests/test_mcp_integration.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_mcp_integration.py)：schema/description/output 上限、annotation mapping、remote errors、namespace、timeout/cancel 等。
- [tests/test_failure_harness.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_failure_harness.py)：hook、permission、tool failure、cancel、provider retry、termination。
- [tests/test_trace_v2.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_trace_v2.py)：tool lifecycle、permission decision、trace redaction、repository effect correlation。
- [tests/test_github_issue_delivery.py](https://github.com/Napabana/forge-agent/blob/e672dfd00de855d6666f0a13ca5f54eb087adbd6/tests/test_github_issue_delivery.py)：GitHub Issue delivery 与 git mutation registry ownership。

### 本轮没有找到覆盖的关键回归

| 新测试 ID | 输入 | 预期 |
|---|---|---|
| git_diff_shell_metachar | git_diff path=";id" | 不发生 shell 二次解析；argv 中仅保留字面参数 |
| git_add_shell_metachar | git_add paths=[";id"] | 不执行额外命令 |
| git_commit_shell_metachar | git_commit message=";id" | message 作为单一 argv 参数 |
| pytest_shell_metachar | test args=";id" | 不执行额外命令；最好 args schema 改 array[string] |
| strict_read_only_git_diff_injection | read_only=true + git_diff path=";id" | 不能通过工具名白名单绕过 |
| shell_external_read | cat /etc/passwd、env、cwd=/etc | 明确 deny/confirm，或必须处于强制 sandbox |
| strict_read_only_external_cwd | shell pwd cwd=/etc；git_status cwd=other_repo | workspace 外路径拒绝 |
| search_symlink_escape | workspace/secret_link -> outside file | search_text/find_symbol 不得读取目标 |
| search_symlink_internal_skill | repo/link -> .agents/skills/... | 不得绕过内部技能路径保护 |
| schema_nested_validation | array.items / nested required / enum / additionalProperties | 本地 validator 按 schema 拒绝 |
| hook_tool_identity_mutation | PreToolUse 改写 block.name | 拒绝 identity mismatch，不能对另一个 tool 执行 |
| mcp_conflicting_annotations | read_only_hint=true + destructive_hint=true | MAY_MUTATE / CONFIRM |
| mcp_timeout_uncertain | 远端完成副作用后延迟 response | 不盲目 retry 非幂等调用 |
| mcp_multi_server_startup | 多个 server 顺序启动接近 timeout | 全局 startup deadline 正确 |
| trace_manifest_snapshot | native + MCP tools | trace 记录 manifest hash/schema version |

**本轮未运行上述测试，也没有把现有测试标记为通过。** P0 修复建议先使用 fake Runtime 断言 argv，不执行任何危险 payload；之后再用隔离容器做端到端验证。

建议最小离线测试命令（待修复实现后由本地运行）：

    python -m pytest -q \
      tests/test_tool_lifecycle_p0_2.py \
      tests/test_execution_constraints.py \
      tests/test_day3.py \
      tests/test_mcp_integration.py \
      tests/test_failure_harness.py \
      tests/test_trace_v2.py \
      tests/test_github_issue_delivery.py

## 20. 面试必备问答

**Q1：Forge 的 ToolExecutor 做了什么？**  
它统一管理一次工具调用生命周期：取消检查、参数校验、PreToolUse、Permission/Constraints、用户确认、真正执行、PostToolUse，并将错误区分为可恢复 ToolResult 或基础设施失败。它不等于操作系统 sandbox。

**Q2：ToolEffect 和 PermissionManager 有什么区别？**  
ToolEffect 当前描述 repository effect，主要服务 Planning mutation gate；PermissionManager 是执行时 ALLOW/DENY/CONFIRM 策略。当前 PermissionManager 对 shell、workspace file tools、MCP 做专门判断，其他 native tools 默认 ALLOW，因此不能把 ToolEffect 当作完整权限策略。

**Q3：为什么不能只靠 JSON Schema？**  
Provider strict schema 不是所有端点都保证支持；Forge 本地校验当前只检查 required 和顶层 type。真正执行前需要本地完整校验，并且对 shell command 等高风险接口采用 argv，而不是指望 schema 拦住所有恶意字符串。

**Q4：Worktree 和 Docker sandbox 有什么区别？**  
Worktree 隔离 Git 工作结果；DockerRuntime 才提供容器进程、网络、根文件系统和资源边界。默认 sandbox=false 时仍是 LocalRuntime。MCP stdio server 也不在 DockerRuntime 内运行。

**Q5：为什么 git_diff 也会成为安全入口？**  
Git 的 -- 只结束 Git option parsing，不结束 Shell parsing。Forge 当前把 path 拼进 Shell 字符串并用 shell=True 执行；模型可控的分号等元字符会被 Shell 解释。git_diff 还在 strict read_only 白名单中，因此必须改成 argv。

**Q6：为什么 timeout 后不能直接 retry？**  
本地命令 timeout 与远端 MCP timeout 的副作用语义不同。远端操作可能已经完成但响应丢失；重试可能重复部署、创建资源或其他非幂等动作。需要 idempotency key 或 outcome_unknown 状态。

**Q7：Trace 是否证明工具没有越权？**  
不能。Trace 记录权限决策、调用和仓库变化，但 redaction 只在持久化边界，模型可能已经看到原始 ToolResult；repository effect 也只覆盖当前 repo 状态，不能证明没有仓库外读写、网络或远端副作用。

## 21. 阶段结论与下一阶段

阶段 03 的核心结论：Forge 已有可复用的 ToolExecutor 生命周期和较好的 FileTool 最终路径校验，但当前工具执行安全边界存在明确缺口。最重要的不是继续增加权限开关，而是确保每个 native tool 的参数在进入 OS/process boundary 前保持结构化、路径和 cwd 被约束、权限决策覆盖真实执行身份。

**P0 remediation 应独立于架构审计**：先将 Git/Pytest 的执行参数改为 argv，并补齐 fake runtime regression；随后修复 Search symlink、cwd/workspace boundary、Shell external reads 和 native ToolEffect policy。没有必要在本轮直接修改生产功能代码。

**下一阶段：04 — Context / History / Token Budget / Compaction。** 依阶段 0 的 D01–D12 逐项核对 canonical ConversationHistory、消息角色/事件引用、token counter、model capability 与 request budget、tool pruning、semantic packet、structured compaction、prepare_next_turn override、chat session persistence，以及不同入口对 history 的实际接线。特别核查工具结果剪枝/摘要是否保留足够的执行证据，是否会把工具输出中的不可信内容提升为系统指令，是否影响 Teacher→Student trajectory 的可重建性。输出 04 报告并更新索引/决策记录。

**本轮没有修改任何生产功能代码、评测配置或数据集；没有运行 Forge pytest；没有调用收费模型或远端 MCP 服务。**
