# 阶段 02：Model Integration、Agent Loop 与工具调用协议横向审计

> 审计日期：2026-11-12。Forge 当前 `dev` 当轮开始 HEAD：`5469008146c83a25124d2bc6c79683113ea262a1`；与阶段 01 交接提交相同。
> **功能源码基线**：`Napabana/forge-agent@3e3fc48814aa15604c8a3a8a4432563854f8b6c1`（基线之后仅已跟踪研究文档变化）。
> 外部对照源码固定：`earendil-works/pi@b729499585bd7dacf6e9bc52994c6f157e35f9c9`、`openai/codex@1f1f6ebb6c156d5ab1d4745155f340b897b24f02`；Claude Code 仅以 2026-11-12 官方文档为依据。
> **证据性质**：本轮直接读取生产源码、部分历史设计记录、多个测试模块及两套开源 Agent 源码；查阅 Claude 官方文档；**未运行 pytest、真实 Provider、Docker 或 Benchmark**。本报告中的风险分级是静态审查后的建议，不是假称已经复现缺陷。

## 0. 执行摘要

阶段 02 对应阶段 0 的 **B01–B12，全部进入本轮对标**。结论不是“Forge 功能比不上成熟 Agent，必须多加功能”，而是：

1. Forge 的核心 `LLMBackend → LLMResponse(Action) → Agent.run → ToolExecutor → Observation` 实现简单，代码与测试基础比较完整；适合小模型、顺序 Coding Task。但它形成了一个强约束：**每次模型响应仅保留一个 Action**。
2. Forge 的三条主要模型适配路径——Anthropic Messages、OpenAI-compatible Chat Completions、OpenAI Responses——在原生消息含多个 Tool Call 时**仅提取第一个**。请求侧也未在已审阅的构造函数中明确要求仅生成一个。这不等于并行工具是训练的必要功能，但存在模型输出与 Agent 消费协议的不一致。**更小的修复路线：先禁止批量生成或显式拒绝批量，不需要立即实现 parallel tools**。
3. 在 OpenAI-compatible streaming 的 `_stream_with_tools` 中，`_to_openai_tool(t)` 未传 `strict=self._strict_tool_schema`，与非 streaming 路径不一致；同时只要收到任何 tool-call delta，就把流响应的终止原因归一化成 `tool_calls`，**即使原始 `finish_reason=length`**。这两点是可静态确定的代码行为，风险影响需补模拟响应测试。
4. OpenAI Responses 的解析先寻找 `function_call`，并没有在执行前拒绝 `status=failed/incomplete` 的响应；`stream` 对 `response.failed` / `response.incomplete` 也进入相同解析。是否会造成实际副作用，取决于失败响应含有怎样的 output；建议用 fake response 做安全边界测试。
5. Forge 的 Model history 通常是**文本化 Action + 文本化 Observation**，不是按原生 `tool_call_id` 和 `tool_result` 配对的 provider transcript。当前方法能跨后端使用，但可能影响函数调用对齐、轨迹保真度与小模型后训练；应优先通过统一模型/任务的 A/B 测量决定是否修改，不要为了贴近 Pi 就立即整体替换 History 数据结构。
6. 单独的 Provider 错误分类、有限重试、Retry-After、Stream 已有输出后不自动重试、cooperative cancel，以及 Completion Guard/Loop Detection 属于合理的可信执行设计。建议**保留**。

**当前阶段训练准入意见**：在正式冻结 2B/4B Teacher/Student 基线前，建议优先做多工具输出、流式中断/截断和 schema 配置一致性的定向离线测试；不必先增加 Subagent、并行工具执行或全异步 Agent Loop。

## 1. 实际源码与调用关系

### 1.1 Source map（Forge）

| 子层 | 核心源码及函数 | 职责 |
|---|---|---|
| 公共接口 | [`llm/base.py::LLMMessage/LLMResponse/LLMBackend`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/base.py#L27) | 模型无关消息、一个 Action、Provider usage、complete/stream |
| Provider Router | [`llm/router.py::create_backend`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/router.py#L78) | provider/protocol 选择、ModelCapabilities、strict capability |
| Context Limits | [`llm/capabilities.py::ModelCapabilities`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/capabilities.py#L9) | context_window、model max_output，未知时显式 None |
| Anthropic | [`llm/anthropic_backend.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/anthropic_backend.py) | Anthropic Messages、`tool_use`、stop_reason、stream |
| OpenAI-compatible | [`llm/openai_compat.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py) | Chat Completion、function calling / text-only 兼容、tool deltas |
| OpenAI Responses | [`llm/openai_responses.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_responses.py) | Responses protocol、`function_call` / output_text、stream events |
| Schema Adapter | [`llm/tool_schema.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/tool_schema.py) | strict capability、nullable optional、schema transform |
| Token Accounting | [`llm/usage.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/usage.py) | usage、cache subset、不重复计算 reasoning |
| Retry Classification | [`llm/errors.py::classify_llm_error`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/errors.py#L36) | Auth/BadRequest/RateLimit/Timeout/Connection/Server/Callback |
| Domain Events | [`agent/task.py::Action/Observation/RunStatus`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/task.py#L88) | 模型动作、工具结果、终止状态 |
| ReAct Main Loop | [`agent/core.py::Agent.run`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L233) | turn、LLM call、control、tool、guard、终止 |
| Retry/Tracing | [`agent/core.py::_call_with_retry`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1701) | bounded retry + streaming callback + cancel |
| Prompt/Reflection | [`agent/prompt.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/prompt.py) | 系统说明、任务说明、反思/资源提醒 |
| Loop Guard | [`agent/loop_detector.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/loop_detector.py) | period/repeats/repository/test progress |
| Tool Result Path | [`harness/executor.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/harness/executor.py) | 统一执行工具、permission/cancel/hooks；下一阶段细审 |

### 1.2 数据与控制流

```text
ExecutionRunner.run(request)
    └─ Agent.run(Task, EventLog, optional shared History)
         ├─ 建 PlanningRuntime / RecoveryRuntime / SkillRuntime
         ├─ canonical History + TokenBudget + query-aware Repo Map
         └─ for step=1..task.max_steps:
              ├─ prepare_next_turn（step>1）
              ├─ build_system_prompt + trim visible History
              ├─ active_schemas = native + planning + skill
              ├─ _call_with_retry() 
              │      └─ backend.complete() 或 backend.stream()
              │          ├─ provider request/response translation
              │          └─ LLMResponse(action: 仅一个 Action, usage, raw)
              ├─ provider usage/Trace、cancel boundary
              ├─ normalize_optional_nulls(action.params)
              ├─ log Action（canonical）
              ├─ 控制分支：skill_load / plan_create/update/revise
              ├─ FINISH → guard → SUCCESS 或 feedback
              ├─ GIVE_UP → GAVE_UP
              ├─ TOOL_CALL → Planning gate → ToolExecutor
              │           → Observation / repository / tests / progress
              │           → Action + Observation 文本写入 History
              ├─ Loop Detector / Recovery / Reflection
              └─ max_steps → INCOMPLETE
     └─ Runner 独立 Acceptance + final trace
```

注意：`_active_tool_schemas` 会将 Planning/Skill 的控制命令暴露给模型，但它们在 `Agent.run` 中被控制分支消费，不经过普通原生工具的完整运行路径。**不要把所有“schema 可见项”当成可执行原生 Tool**。此专题只记录接口，Tool governance 留阶段 03。

## 2. 逐项审计（B01–B12）

### B01：LLMBackend / Message / Response / Usage

[`llm/base.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/base.py#L27) 定义 `LLMMessage(role, content, tool_call_id, event_ref)`、`LLMToolSchema(name, description, parameters)` 和 `LLMResponse(action, raw_content, usage)`。Provider 封装需要将原生回答转为**一个**领域 `Action`。 `MockBackend` 允许完全离线模拟执行。默认 `LLMBackend.stream` 回退到 `complete` 并调用 `on_text`；不是所有 Backend 都保证真正 token-level streaming。

**优点**：Agent 核心不引用任何具体 SDK，测试不需要 API；Usage 通过 TokenUsage 统一字段（`cached_tokens`、`cache_write_tokens` 是 input 子集，`reasoning_tokens` 是 output 子集，不能再次加入 total）。  
**代价**：单 Action 容器无法保持同一模型消息中的多个 `tool_calls`、各调用 ID、provider stop_reason 和批量状态。这是与 Pi/Codex 可验证的最关键结构差异。

### B02：Provider/Protocol 路由与能力声明

[`llm/router.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/router.py#L78) 支持 `anthropic/openai/deepseek/groq/ollama`，区分 `anthropic`、`chat_completions` 和显式 `responses`。OpenAI-compatible 服务允许覆盖 `base_url`。模型能力 `ModelCapabilities` 的 `None` 明确代表未知；`request_max_output_tokens` 与 `model_max_output_tokens` 分开。[`config/schema.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/config/schema.py) 留有兼容旧 `max_tokens` 的桥接。

**好处**：第三方模型 API 可以接入现有 Harness；小模型实验可切换端点。  
**限制**：route 是配置驱动而非真实 capability negotiation；`protocol=auto` 在 OpenAI-compatible 分支默认为 Chat Completions，不会自动检测后端支持 Responses。`strict_tool_schema=auto` 在 [`llm/tool_schema.py::resolve_tool_schema_capabilities`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/tool_schema.py#L31) 采取保守 `False`（source 中标明 unverified）。不能误称“Forge 自动识别任何模型所有能力”。  
**训练要求**：记录模型、协议、端点类别、tool schema、max output、context window、所有后端 fallback 状态。

### B03：Anthropic Messages

[`llm/anthropic_backend.py::_parse_anthropic_response`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/anthropic_backend.py#L214)：`stop_reason=tool_use` 遍历 content blocks，**遇到第一个 tool_use 就返回** TOOL_CALL；`end_turn` 且有 text 直接 FINISH；其余（包括 `max_tokens`）GIVE_UP。stream 使用 Anthropic SDK context manager，分发 text_delta/thinking_delta，最后取完整 message 再解析。

**设计取舍**：直接以模型输出终止语义定义 Action，简单明确；但一个消息可能同时包含多个 tool_use blocks，未消费的调用没有进入 Trace/History。文本 end_turn 为完成声明，是否真完成依赖 Agent Completion Guard 与外部 Acceptance，而非 Provider 的终止原因自身。

### B04：OpenAI-compatible Chat Completions

[`llm/openai_compat.py::_complete_with_tools`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L100)：以 `tools` 和 `tool_choice=auto` 调模型；[`_parse_openai_response`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L334) 在 `finish_reason=tool_calls` 下**只取 `tool_calls[0]`**；正常 `stop` 优先识别 `GIVE_UP:`、伪工具文本，再识别 FINISH；`length` 在普通非流式路径落入 GIVE_UP。兼容网关把 `finish/give_up` 错包成 function call 时会归一化为 terminal Action。

仅通过模型名称前缀 `deepseek-reasoner` / `deepseek-r1` 进入 text-only 路径：[`_complete_text_only`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L153) 在 system prompt 注入 schema 的文本列表，解析工具 JSON、TASK_COMPLETE、GIVE_UP；对未知模型的 capability 并不主动探测。通过普通 `stop` 输出的 `Action: ... Params: ...` 伪 tool call 也可被恢复。

**重要**：text-only 解析最后还有关键词启发式（`task complete`、`all done`、`cannot solve` 等），这些关键词不是真实完成证据；外部 evaluation 必须检查 repo tests。

### B05：OpenAI Responses

[`llm/openai_responses.py::_parse_response`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_responses.py#L194) 从 output 查找**第一个** `function_call`；无函数调用时，只要有 `output_text` 就构造 FINISH；均无时返回 GIVE_UP。支持 input items 和 streaming event delta；stream 收到 `response.completed` / `response.failed` / `response.incomplete`，均设置 `final_response`，随后调用同一 `_parse_response`。

**风险 B05-R**：上面 parser 没有显式以 status 为前置条件拒绝 output 中的 function_call。因此若上游标记 failed/incomplete 但仍有可解析 tool call，可能继续走 TOOL_CALL 进入执行；需要测试构造模拟响应验证，而不是推测真实 API 必然如此返回。

**现有测试证据**：[`tests/test_openai_responses.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_openai_responses.py) 包括纯文本完成、函数调用、usage、tool result 转换、stream delta 和“无 completed 事件”的 fallback；本轮阅读的测试名/相关代码**没有提供上述 failed/incomplete + tool call 的确认结果**。

### B06：Strict Tool Schema 与可选参数兼容

[`llm/tool_schema.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/tool_schema.py) 把非 required 对象字段变成 required-but-nullable，`additionalProperties=False`，同时通过 `normalize_optional_nulls` 在 ToolExecutor 前剔除多余 null placeholder，实现**Provider strict schema 与本地 Tool Schema 相互兼容**。

**已确认不一致（B06-R）**：[`OpenAICompatBackend._complete_with_tools`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L100) 调 `_to_openai_tool(t, strict=self._strict_tool_schema)`；而 [`_stream_with_tools`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L582) 使用 `_to_openai_tool(t)` 默认 strict=False。因此 `strict_tool_schema='on'` 的配置在 streaming 模式下没有被等价执行。**这是静态代码事实，建议补离线 request capture 测试，并做最小差异修复**；不需要重构 ToolRegistry。

### B07：LLM 错误分类、重试、退避与 Cancel

[`llm/errors.py::classify_llm_error`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/errors.py#L36) 把 auth/ordinary 4xx/callback 视为不可重试，把 429、408/409、timeout、connection、5xx/overloaded 视为可重试（依据状态码、异常类型和消息）。[`agent/core.py::_call_with_retry`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1701) 实现限定次数、`Retry-After`、指数退避/抖动和 cancel wait。

特殊安全点：**一旦本次 Stream 有任意 text/thought chunk，就不再重试**，避免 Provider 重新生成导致前端重复内容/状态不一致。用户回调异常被包装为 `LLMCallbackError` 且不可自动重试。同步 Provider 在调用期间不能强制终止，返回后有 cancel safe boundary。

**评价**：这比简单 try/except 更接近可用于真实任务的可靠性设计，应保留。测试文本 [`tests/test_failure_harness.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_failure_harness.py) 已包含 transient retry、retry exhausted、nonretryable、cancel during retry 等场景；本轮未执行。

### B08：ReAct Main Loop、Action、Observation

[`agent/core.py::Agent.run`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L233) 对每次任务创建新的 Planning/Recovery/Skill 控制状态，构建 system prompt/裁剪 History，调用 Backend 得到一个 Action，分别处理 control/FINISH/GIVE_UP/普通 Tool Call。工具调用先经 Planning/Recovery gate，再进入 ToolExecutor，更新 semantic-progress、test-state 和 Trace。

**顺序单工具 Loop 是合理的简化，不等于当前接口已经强制模型只输出一个工具**。模型如果输出多调用，三个 Backend 均截断为第一个；当模型需要执行 N 次工具操作时，可能需要 N 次模型调用才能继续下一个。小模型可能因此增加 token 成本，但不能凭源码推断增加多少，必须测量。

**History 重要事实**：[`Agent._format_action_for_history` / `_format_observation_for_history`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1683) 将一次工具操作追加为：
```text
assistant: Thought: ...
           Action: file_read
           Params: {"path": "x.py"}
user:      [Tool: file_read | SUCCESS]
           ...
```
这是跨 Provider 的**文本化交互协议**。虽然 `LLMMessage` 和 Backend Converter 具备 `tool_call_id` 支持，[OpenAI converter](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L204)、[Anthropic converter](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/anthropic_backend.py#L117)、[Responses converter](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_responses.py#L159) 可生成原生 tool result，但主循环对普通工具操作**不存储并重放原生 tool_call_id 配对**。后者代表 `Action/Observation` 会话内容，而不是 Provider 原始 transcript。要做 SFT，必须明确训练目标是 Forge 的这种文本/Action 协议，还是原生函数调用 token 协议。

### B09：Streaming（分段文本、reasoning、tool deltas）

- [Anthropic stream](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/anthropic_backend.py#L277)：实际 text/thinking deltas，final response 再解析。
- [OpenAI chat stream](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L562)：按 index 拼接 tool arguments，分发 text/reasoning；通过 `mock_choice` 复用非流式 parser。
- [Responses stream](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_responses.py#L59)：记录 text/reasoning，拼接 arguments，支持缺失 response.completed 的 fallback。
- [Core callback](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1701)：把 callback failure 归类为 nonretryable。

**第二处明确协议不一致（B09-R）**：OpenAI Chat stream 用 `normalized_finish_reason = "tool_calls" if tool_calls_raw else (finish_reason or "stop")`。若上游确实返回 `finish_reason="length"`，只要已有 tool delta，就会以 tool_calls 继续解析；而非 streaming 的 `length` 明确 GIVE_UP。使用 [`_parse_openai_response`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/llm/openai_compat.py#L334) 的结果可能进入 ToolExecutor。即使本地 Tool 参数校验能阻止一部分无效 JSON，仍不能把这个行为当作完整的“响应已完成”安全前置条件。

**推荐行为**：任何已声明长度截断的输出，不执行对应工具；返回可追踪的失败/重试建议。参考 Pi 在 [`agent-loop.ts::failToolCallsFromTruncatedMessage`](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts#L488) 对整个截断工具批次返回错误 Observation，不执行有潜在副作用的工具。

**边界**：这属于局部协议安全性，需要模拟响应测试；不意味着现在已经真实执行过危险命令。

### B10：Prompt 组装、Schema 动态性与 Reflection

[`agent/prompt.py::build_system_prompt`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/prompt.py#L75) 集中维护 workflow、rules、tool descriptions、repository summary 和 trusted execution constraints 说明。[`Agent._active_tool_schemas`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1529) 收集工具/Planning/Skill 控制；[`Agent._render_request_parts`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1539) 组合 Repo Map、Plan、Recovery、Skills、readonly 约束；[`Agent._build_messages`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L1593) 将 system + visible history 组装，并执行 token trimming。

当剩余步骤 ≤3，会插入临时资源提醒，**不写回 canonical History**；Completion Rejected、Loop/Test Failure、No Progress 等反馈会追加到 History，作为后续决策条件。

**合理性**：策略信息从代码可靠地进入模型上下文；Planning/Skill runtime 事实不会完全由 LLM 自我声明。  
**待测问题**：复杂固定 system prompt + 大量 tools 在 2B/4B 上的 prompt adherence、token 代价和工具选择准确率；这属于阶段 04/09 的实验，不凭 prompt 的长度直接定罪。

### B11：Loop Detector 与 semantic progress

[`agent/loop_detector.py::LoopDetector.observe`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/loop_detector.py#L95) 对 Action/Observation 做 canonical fingerprints，按 period 识别循环，并结合 repo state 与 test state 判定是否出现实质进展；第一次循环可引入 Reflection/Recovery，再重复则可终止为 INCOMPLETE。[`agent/recovery.py::SemanticProgressTracker`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/recovery.py#L55) 去重已经观察过的 test/tool evidence，避免“重复同样的测试输出”无限被记为进展。

**好处**：解决 Coding Agent 反复 read/test/edit 的常见问题，降低调用预算浪费。  
**待证**：短窗口是否误伤合法重复操作、不同模型是否因更差的策略更容易触发；测试文本 `tests/test_loop_detector.py` 涵盖 AAA/ABAB/ABCABC、progress/cycle，但本轮没有重跑。该模块深审阶段 06。

### B12：FINISH、GIVE_UP、FAILED、CANCELED、INCOMPLETE

[`agent/task.py::RunStatus`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/task.py#L103) 定义 `SUCCESS/INCOMPLETE/FAILED/MAX_STEPS(兼容)/GAVE_UP/CANCELED`。[`Agent.run` FINISH 分支](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/agent/core.py#L622) 比对实际 repo 内容变化、require_tests、latest test pass 和 test freshness，并检查未解决 fatal infrastructure；不满足条件则给模型 Completion Rejected 提示，而不是直接成功。达到 max_steps 为 INCOMPLETE；主动 GIVE_UP 为 GAVE_UP；Provider 终止错误为 FAILED；外部取消为 CANCELED。

**要区别 Provider stop 与 Task completion**：某个模型响应 `end_turn` 或 `stop` 只构造候选 FINISH，能否变成 SUCCESS 还要通过 Completion Guard。即便成为 SUCCESS，Runner 的独立 hidden acceptance 仍可能失败，Stage 08/09 要复核统计口径。

**部分代码设计取舍**：`end_turn/stop` 普通正文几乎都可成为候选 FINISH，而依赖 `Task.require_changes/require_tests` 和测试行为阻止早停。如果任务被误归类为“非代码修改任务”，可能出现提前 FINISH；这不是 Provider adapter 单独能完全解决，阶段 08 要对 Task intent inference 与 independent verifier 进行组合测试。

## 3. Pi、Codex、Claude Code：源码/官方行为的逐功能对标

### 3.1 Pi Agent（可审计真实代码）

固定版本源码：
- [`packages/agent/src/agent-loop.ts`](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts)：`runLoop` / `executeToolCalls` / `failToolCallsFromTruncatedMessage` / `createToolResultMessage`。在一次 assistant response 的 `content` 中遍历所有 toolCall block，按 `config.toolExecution` 和工具自身 `executionMode` 选择顺序/并行；明确处理 `stopReason=length`，不运行截断的工具。
- [`packages/agent/src/agent.ts`](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent.ts)：`Agent` 的 Model/Tools/State/Abort/queue/turn hooks。该固定版本 `toolExecution` 默认为 `parallel`，但工具可显式指定 sequential。
- [`packages/coding-agent/src/core/agent-session.ts`](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/coding-agent/src/core/agent-session.ts)：coding-specific session，compaction/retry/extension/steer；与 `packages/agent` 的通用核心分开。

**相同问题/不同实现**：Forge 将工具输出文本化后给下一轮模型；Pi 将原生 Tool ResultMessage 与 toolCallId 按顺序加入 session context。Pi 的批量执行策略功能更全，但设计成本（多工具冲突、副作用、权限、事件排序）也更高。**本阶段建议 Forge 优先补“单调用契约一致”而非照搬 Pi 的并行默认值。**

### 3.2 Codex CLI（可审计真实代码）

固定版本源码：
- [`codex-rs/core/src/session/turn.rs::build_prompt`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/session/turn.rs#L1840)：模型 Prompt 包含 `parallel_tool_calls: true`，结合一轮的事件流处理模型输出项。
- [`codex-rs/core/src/tools/parallel.rs::ToolCallRuntime`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/parallel.rs#L49)：工具调用运行时与取消 token/工具执行状态；`handle_tool_call` 把单个调用转为异步处理 future。
- [`codex-rs/core/src/tools/router.rs::tool_supports_parallel`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/tools/router.rs#L269)：根据 Tool Registry 的能力判断是否支持并发工具调用。不能因为 Prompt 开放 parallel=true 就误称“Codex 无条件让所有工具并行执行”。
- [`codex-rs/core/src/state/turn.rs::TurnState`](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/state/turn.rs)：Turn 内待审批、取消、输入和调用数量是显式状态。

**设计思想对比**：Codex 的模型请求、Tool Router、Turn Runtime 使用不同层的 typed state；Forge 的单 `Action` + 同步 step 降低实现复杂度，但不能原样支持 provider-native multi-call 与更丰富的事件重放。

### 3.3 Claude Code（官方公开语义，不是内部源码）

2026-11-12 官方文档核实：
- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)：公开描述 gather context → take action → verify 的 agentic loop、工具观察结果再决策。
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)：Agent SDK/CLI 使用相同工具、loop 和 context，`claude -p` 为非交互模式；输出状态、允许的工具有显式 CLI 选项。
- [Stream responses in real-time](https://code.claude.com/docs/en/agent-sdk/streaming-output)：SDK 能选择完整 AssistantMessage 或 partial streaming，原始 `StreamEvent` 包含 `content_block_delta`、tool use 等事件。
- [Agent SDK agent loop](https://platform.claude.com/docs/en/agent-sdk/agent-loop)：公开 Agent SDK 语义。如在线链接后续变化，以本次核验日期为准。

**限制**：官方资料没有给出 Claude Code 内部工具批次调度器、所有 StopReason 分类、Retry 策略的完整源码，本报告不对其隐藏代码作断言。能明确的只有其公开 Agent SDK 的行为契约，以及与 Forge 具可比性的 Loop/Tool/Streaming 功能。

### 3.4 B01–B12 对比矩阵

| ID | Forge 当前行为 | Pi 开源源码对照 | Codex 开源源码对照 | Claude 官方公开依据 | 结论 |
|---|---|---|---|---|---|
| B01 LLM 抽象 | `LLMResponse(action)` 单动作 | Core AgentMessage + per tool results | Typed ResponseItems/Turn | Agent SDK 消息 | 设计较简，批次信息被压缩 |
| B02 Provider Route | 明确 Provider/Protocol/Capabilities，未知不猜 | pi-ai model/provider adapter | Model client session | SDK model 配置 | Forge 适合 proxy，但 capability 依赖配置 |
| B03 Anthropic | 首个 tool_use / end_turn→FINISH | toolCall content 全部执行 | Model response items（实现不同） | Claude 使用 tools/loop | 多工具存在协议缺口 |
| B04 Chat Completions | 首个 tool_calls[0] / stop→FINISH | 所有 toolCall 按策略执行 | 接受多 OutputItem | SDK 非等价接口 | 推荐 single-call contract 先行 |
| B05 Responses | 首个 function_call；状态前置校验不足 | pi-ai 能对接不同 API（未逐 Provider 审计） | 主模型 Responses/typed outputs | 不可推断内部 | 失败/未完成消息需模拟边界 |
| B06 Schema | strictify + nullable normalization | Tool arguments validation | ToolRouter/SpecPlan | 工具类型公开 | 结构合理，stream 配置不一致 |
| B07 Retry | 分类、有限重试、cancel、partial stream 不重试 | AgentSession retry/Abort | Turn 错误恢复/取消 | SDK 中断/错误行为公开 | 设计值得保留 |
| B08 ReAct | 单 Action 同步 step | async runLoop/多 call | async run_turn/ToolCallRuntime | gather/action/verify | 小项目合理但有限制 |
| B09 Stream | text/thought/tool argument delta | streaming event + tool results | streamed ResponseItem | SDK partial StreamEvent | Forge stop_reason 保护有缺口 |
| B10 Prompt | template + tool schemas + runtime ctx | AgentSession prompt/request projection | Prompt/StepContext | skills/tools context 公开 | 不同抽象，相同目的 |
| B11 Loop Guard | period+repo/test progress | core turn hooks/可外加策略（不以本次片段判等价） | Task/Turn state（不同） | adaptive loop（公开） | Forge 有明确主动守卫 |
| B12 Termination | FINISH guard/INCOMPLETE/GIVE_UP/CANCELED | stopReason+finishTurn+agent_end | Turn outcome/error/cancel | CLI/SDK result/error | 多层状态设计合理 |

未公开或本轮未读到的对照实现标记为“不同/未核查”，**不意味着对方没有功能**。

## 4. 高价值问题清单：源码事实、风险和建议

### R02-01：多工具调用悄然丢失（优先 P1，先测试/限制生成，不立即开发并行）

**确切路径**：

- `llm/base.py::LLMResponse.action` 只有一个 `Action`；
- Anthropic `_parse_anthropic_response` 遇首个 tool_use 直接 return；
- OpenAI Chat `_parse_openai_response` 访问 `tool_calls[0]`；
- Responses `_parse_response` 用 `next(... function_call ...)`；
- `Agent.run` 按一个 Action 执行，无法知道还有未消费工具；
- 当前 Anthropic/Chat/Responses 的已审阅请求构造中未设显式 `parallel_tool_calls=False`（该参数是否受目标兼容网关支持需要验证）。

**威胁模型**：模型发出 A、B 两个工具调用；只有 A 执行。模型的单次请求记录含 B 的意图，但 Forge 的下一轮 History 只包含 A 的结果。若 B 是测试或关键编辑，不会执行，可能增加失败率/重试成本；不是安全越权，因为 B 反而没有执行。

**建议顺序**：1）用 fake response 证明三条 backend 都只取首个，基于 Trace 标明 batch truncation；2）优先请求单 tool call（对可支持的协议发送相关参数），同时在消费侧遇多调用时 fail-closed 或明确 feedback；3）只有完成 baseline 发现明显瓶颈时，才实现“顺序执行批次+每个结果/ID 配对”；4）并行读工具可作为 P2 试验，写工具不要直接并行。

**为什么不立刻上多调用**：小模型有限 context/budget，多个工具读写依赖和取消/权限顺序更难验证；单工具协议只要明确、受控、可复现，也能建立合格 baseline。成本：协议测试 S、单调用收口 S～M、完整 batch sequential M～L、parallel L。

### R02-02：OpenAI-compatible streaming strict schema 配置漂移（P1，小修）

`_complete_with_tools` 使用 `strict=self._strict_tool_schema`；`_stream_with_tools` 调 `_to_openai_tool(t)` 默认非 strict。配置 `strict_tool_schema=on` 时，用户认为启动的 strict 实际因 `stream=True` 变化。对第三方 2B/4B 端点尤其需要记录，因为 strict 支持可能不是所有网关一致。

**最小复现**：同一个 fake backend/tool schema（含 optional 字段），分别走 complete/stream，截获 create kwargs，断言 strict flag、required/nullable、additionalProperties 一致。成本 S，建议 Baseline 前修复，不影响不使用 strict 的变体。

### R02-03：Chat Completions 流式 `length` 仍有工具增量时被转成 `tool_calls`（条件 P1）

在 `_stream_with_tools` 最终强行设置 finish_reason 为 tool_calls（只要 `tool_calls_raw` 非空）；原始 `finish_reason="length"` 被覆盖。非流式 parser 对 length 是 GIVE_UP；Pi 会为截断 tool calls 返回错误并要求重发，不执行。

**复现设计**：构造 stream delta：一条完整/不完整的 function arguments + `finish_reason=length`，断言不会触发任何 ToolExecutor 副作用；同时用 `finish_reason=tool_calls` 验证正常执行。未跑回归，因此“可能触发”是静态推导。若用户训练使用 `stream=True` 的 Chat Backend，优先修复。成本 S。

### R02-04：Responses failed/incomplete 仍可能被解析成可执行 `function_call`（条件 P1）

`_parse_response` 先找 function_call，而没有统一检查 response.status；stream 收到 `response.failed`/ `response.incomplete` 后一样取 final_response 解析。**必要测试条件**：上游返回 `status="incomplete"` 且 output 中已有可解析 function_call，需断言不执行。不同 Provider 对 failed/incomplete 的 output 语义可能不同，故不宣称所有上游一定产生这种形态。成本 S。

### R02-05：文本化 Action/Observation 与原生 Tool Transcript 不等价（P2 研究议题）

`_format_action_for_history` 和 `_format_observation_for_history` 将 native tool call 转为 assistant/user 文本；即使 `LLMMessage.tool_call_id` 允许原生序列化，当前 Agent.run 主链并不保存/使用它。对 Forge 既有任务是一致的内部协议，利于接口简化；对 Teacher/Student 训练需要定义标准 schema：

```json
{
  "request": {"messages": "model-visible snapshot", "tool_schema": "exact manifest"},
  "decision": {"action_type": "tool_call", "name": "file_read", "params": {"path": "x.py"}},
  "observation": {"status": "success", "tool_name": "file_read", "output": "..."},
  "result": {"agent_status": "...", "acceptance_status": "...", "grader": "..."}
}
```

该示例是**建议的数据结构，不是 Forge 已实现的数据导出格式**。不要将其写成“Forge 直接支持原生 assistant tool_call token SFT”。要做真实比较，固定相同 Teacher、工具、任务，比较文本协议与 native tool pairing 的 action parse rate、tool success、tokens、任务成功率。未经数据支持不建议改造。

### R02-06：文本 stop→FINISH 与错误完成声明（P2，外部验收重要）

三条 backend 的普通文本结束都偏向 FINISH；`Agent.run` 有 require_changes、require_tests、test freshness 等完成性校验。对于只读任务，文本结束往往是合理终止；对于要求代码修改的任务，若 `Task` 意图分类未要求必要验收，可能产生“模型说完成但任务没完成”。应以 hidden grader 衡量，而不是把 stop 直接等同正确。

### R02-07：Streaming callback 与终止分类的微妙代价（P2）

`_call_with_retry` 在 partial stream 已经出字后阻止 retry，避免重复流式输出，这是优点；同时某些偶发网关中断可能导致本轮 Agent FAILED 而不是自动重试，需在基准中**明示 provider failure 与模型策略失败是两种不同统计口径**。尤其若 Teacher 运行在稳定 API、Student 运行在低可靠性代理，对照结果可能被网关差异污染。

## 5. 暂定 10 分制评分（静态证据支持度，不是测试结果）

分值分配：设计 2、实现 3、安全可靠 2、实验证据 2、可扩展 1。当前证据：**已读源码与测试用例文本，未执行测试**；因此所有分数置信度仅中或中低，不能按分数推断实际成功率。

| ID | 功能 | 设计/2 | 实现/3 | 安全/2 | 证据/2 | 扩展/1 | 暂分/10 | 置信度/影响 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| B01 | Backend/Message/Response 抽象 | 1.7 | 2.1 | 1.5 | 1.0 | 0.7 | **7.0** | 中：单 Action 约束 |
| B02 | Provider/Protocol/Capabilities | 1.8 | 2.4 | 1.5 | 1.1 | 0.8 | **7.6** | 中：配置驱动、unknown 保护 |
| B03 | Anthropic Messages | 1.5 | 2.0 | 1.4 | 1.0 | 0.7 | **6.6** | 中：首个 tool_use |
| B04 | OpenAI Chat Completions | 1.6 | 1.9 | 1.1 | 1.0 | 0.8 | **6.4** | 中：multi-call/stream 分歧 |
| B05 | OpenAI Responses | 1.6 | 2.0 | 1.1 | 1.0 | 0.8 | **6.5** | 中：failed/incomplete gate 待测 |
| B06 | Tool Schema strict/nullable | 1.8 | 2.0 | 1.5 | 1.0 | 0.8 | **7.1** | 中：stream flag 漂移 |
| B07 | Retry / Provider error / cancel | 1.9 | 2.6 | 1.7 | 1.3 | 0.8 | **8.3** | 中：测试文本较充分，未执行 |
| B08 | ReAct 主循环 | 1.8 | 2.5 | 1.5 | 1.2 | 0.7 | **7.7** | 中：结构清晰但同步/单 Action |
| B09 | Streaming/partial/deltas | 1.5 | 1.9 | 1.0 | 0.8 | 0.7 | **5.9** | 中：截断流需要 fail-closed |
| B10 | Prompt/Schema/Reflection | 1.7 | 2.3 | 1.4 | 1.0 | 0.7 | **7.1** | 中：应测 2B/4B adherence |
| B11 | Loop/progress/guard | 1.8 | 2.5 | 1.6 | 1.1 | 0.8 | **7.8** | 中：误伤率未实测 |
| B12 | 终止与 Completion Guard | 1.8 | 2.5 | 1.7 | 1.2 | 0.8 | **8.0** | 中：Task 意图推断 + hidden grader 待测 |

> 一些项得分较低，**仅说明当前证据下的局部协议风险**。不能把 12 项简单平均作为 Forge 总评分，也不能与 Pi/Codex/Claude 未采用相同测试任务的项目比较成功率。

## 6. 改进排序：对实习项目的 ROI 而非功能堆叠

| 顺序 | 行动 | 依据 | 投入估计 | Benchmark 准入 |
|---|---|---|---|---|
| 1 | **单调用契约和批量响应测试** | R02-01 | S～M | 基线前建议明确；产生多调用的模型优先 |
| 2 | **Stream strict flag 对齐** | R02-02 | S | 使用 strict 时优先修 |
| 3 | **Chat stream `length` 非执行测试/修复** | R02-03 | S | 流式 Tool Calling 用于训练时建议必修 |
| 4 | **Responses status gate 测试/修复** | R02-04 | S | 使用 Responses 端点做训练时建议必修 |
| 5 | **决定 Teacher/Student 轨迹协议格式** | R02-05 | S（方案）/M（adapter） | 正式 SFT 生成之前必须冻结 |
| 6 | **provider failures 单列统计** | B07/09 | S | 用于公平比较，推荐 |
| 7 | **模拟 2B/4B 早停/循环消融** | B10–12 | M（评测运行费用另计） | 核心 benchmark 应验证 |
| 不做 | 立即完整并行工具执行 | Pi/Codex 已有，不等于 Forge 必须 | L | 无实验证据不做 |
| 不做 | 全量 async Loop/多 Agent 重构 | 与阶段 01 一致 | L | 当前不做 |

优先级：**P1** 指推荐在正式训练 baseline 前收口的协议风险/配置一致性；**P2** 表示先测再决定；**P3** 表示当前没有价值证明。这里没有给出已复现 P0。S ≈ 半天至一天；M ≈ 数天；L ≈ 一周或更长，属于粗估，不是工期承诺。

### 6.1 只需离线 Fake Provider 的测试矩阵

| 测试建议 | 输入/条件 | 期待的可审计结果 |
|---|---|---|
| `multi_anthropic_tool_use` | 同一 `tool_use` response 2 个 block | 显式 reject 或模型请求约束为 1；不再无 trace 地丢弃 |
| `multi_openai_tool_calls` | `finish_reason=tool_calls`，2 个 calls | 同上；Trace 可区分 batch rejected |
| `multi_responses_function_calls` | output 含 2 个 `function_call` | 同上 |
| `stream_strict_tool_schema` | 相同 schema、stream true/false | Tool 参数 schema 严格模式一致 |
| `stream_truncated_tool_call` | tool delta + `finish_reason=length` | 不发生副作用；可归因失败 |
| `responses_incomplete_with_function_call` | `status=incomplete` + call | 不发生工具副作用 |
| `responses_failed_with_function_call` | `status=failed` + call | 不发生工具副作用 |
| `callback_failure_nonretryable` | 输出中回调抛异常 | 不重新请求 Provider，失败有 trace |
| `retry_after_respected` | 429 + Retry-After / cancel | bounded delay，cancel 后不继续 |
| `text_stop_early_finish` | 非修改任务 vs 修复任务 | 未满足 completion guards 不成功 |
| `provider_history_roundtrip` | 工具调用及后继历史 | 对比文本化协议 vs native tool ID 所需的可追踪信息 |

**已有测试文件与本轮阅读范围**：

- [`tests/test_day4.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_day4.py)：Anthropic/Chat parse、usage、tools、max_tokens、text-only fallback。
- [`tests/test_openai_responses.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_openai_responses.py)：Responses parse、stream、usage、router。
- [`tests/test_failure_harness.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_failure_harness.py)：Provider retries、cancel、Tool errors、resource exhaustion、Trace。
- [`tests/test_agent_completion_guards.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_agent_completion_guards.py)：FINISH/Test Freshness/early give_up、伪工具恢复。
- [`tests/test_model_aware_token_budget.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_model_aware_token_budget.py)：模型元数据与 request budget。
- [`tests/test_loop_detector.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_loop_detector.py)：period/duplicate/progress。
- [`tests/test_structured_planning.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_structured_planning.py)：动态 plan schema 与 runtime。
- [`tests/test_trace_v2.py`](https://github.com/Napabana/forge-agent/blob/3e3fc48814aa15604c8a3a8a4432563854f8b6c1/tests/test_trace_v2.py)：调用生命周期事件、usage 与错误状态。

这些文件在 GitHub 当前基线**存在且内容可读取**；本次**没有**实际运行，也尚未完整遍历所有 test 以断言某一 coverage 的完全缺失。“新增测试建议”均为未完成状态。

若用户本地有 `dev` checkout，建议先跑最小离线批次；不在本轮假称执行：

```bash
python -m pytest -q \
  tests/test_day4.py \
  tests/test_openai_responses.py \
  tests/test_failure_harness.py \
  tests/test_agent_completion_guards.py \
  tests/test_model_aware_token_budget.py \
  tests/test_loop_detector.py
```

## 7. 面试必备问答（对应真实项目，不夸大）

**Q1：Forge Agent 与 Harness、LLM Backend 分别是什么？**  
Agent 是 `Agent.run` 的模型决策循环；Backend 适配不同 Provider 的请求/响应；Harness 约束执行环境、ToolExecutor 生命周期、Tracing、Acceptance 和任务失败/恢复语义。Backend 不负责决定代码任务是否真正完成。

**Q2：为什么定义 Action/Observation，不直接使用 Provider 格式？**  
统一 Anthropic/Chat/Responses，不让 Agent Core 依赖某种 SDK，便于 Fake Backend deterministic tests。代价是当前 `Action` 只有一个工具调用，会损失原生批次和 call ID 信息，已列为后续协议审查重点。

**Q3：模型一次返回两个工具调用会怎样？**  
当前三条适配路径通常只处理第一个；第二个不会自动执行/持久化。我们不应该回答“Forge 支持并行调用”。现阶段最小改造是明确限制或拒绝多调用，再以实验决定是否顺序批量执行。

**Q4：为什么不跟 Pi/Codex 一样并行调用工具？**  
Forge 的目标是有限预算、可复现实验。并行带来写冲突、权限/取消、工具结果配对与调试成本；在没有小模型任务成功率收益数据前不值得优先实现。但单调用协议必须正确处理 Provider 输出不符合预期的情况。

**Q5：Streaming 和 Provider Retry 有哪些风险？**  
Forge 的有限重试只对 transient 失败触发，Stream 已经输出任何 chunk 后不再重试，避免重复展示；但 streaming strict schema 当前与普通请求不一致，且截断后的 tool deltas 需要防止执行。这两个是实际源码审查发现的改进点。

**Q6：为什么 Model end_turn 不能代表 Agent 成功？**  
Provider 只声明输出结束，不能验证代码修改与测试状态。Forge 将其转换为候选 FINISH，Completion Guard 检查要求的变更、测试、最终内容是否在测试之后再次改变；Runner 的 hidden verifier 是独立层。

**Q7：Teacher/Student 训练能否直接使用 Trace 做 SFT？**  
Trace 记录真实决策与执行证据，但 Forge 当前训练模型看到的通常是文本化 Action/Observation，不是原生 Provider tool-call 格式。应先定义统一轨迹抽取、数据质量过滤、tool schema 快照和目标输出协议，再决定是否生成 SFT 样本。

**Q8：Provider Token Usage 与 context budget 为什么分开？**  
Token budget 控制模型下次请求的可见上下文与安全余量；Provider usage 是实际调用账单/统计结果。cache read/write、reasoning token 等是输入或输出的一部分，不能重复累加。模型能力未知时要保守估算，不猜上下文窗口大小。

**Q9：做什么能让项目更经得起面试追问？**  
优先展示 3 个 mock-case：模型多调用不被静默丢弃、流式 `length` 不执行截断调用、FINISH 不绕过独立验收。然后用一次可复现的小模型 baseline 和失败轨迹说明“为什么不做并行/多 Agent”。

## 8. 研究证据、限制与交接

本轮**已核对代码路径**：全部 `B01–B12` 所属生产核心文件；已直接抽查相关测试文件及测试名，未执行测试。Pi/Codex 固定 commit 的对应工具运行时与 loop 代码已实际读取；Claude 的结论来自官方在线文档，**不是其闭源内部实现**。外部官方页面可能更新，保留查阅日期。

**需阶段 03 继续验证**：Schema Runtime Validation 是否能阻止所有 malformed/truncated args；原生 11/13 工具具体参数和 ToolEffect；Permission/Pre/Post Hook 与 provider tool batch 的耦合；MCP Tool Schema 是否被正确序列化。

**需阶段 04 继续验证**：Canonical History 与 provider native transcript 的差异；Context Compaction 是否忠实保留工具调用、参数和结果；从非原生 tool transcript 生成 SFT 数据时的 token 格式。

**需阶段 08/09/10 继续验证**：Completion/Acceptance 与 Task intent 的对应、基准 success/abstention 口径、Teacher/Student 的同配置公平性、真实 reward 轨迹抽取。

**下一阶段**：**03 Tool System 与执行治理**，从审计索引 C01–C14 出发：Tool registry 的静态/动态清单、每入口实际工具数量、参数 Schema、ToolEffect、Validation→PreHook→Permission→Tool→PostHook、取消、文件路径安全、Shell/Process、异常分类、原生工具颗粒度、MCP 接入。继续固定外部项目版本，生成 `docs/research/architecture-audit/03-Tool-System与执行治理.md`、更新 README 与 decisions；本阶段发现的多调用、流式截断问题只登记，不在阶段 03 提前修改生产代码。

**本轮不修改源代码、不触发 API 调用、不宣称已通过回归测试。**
