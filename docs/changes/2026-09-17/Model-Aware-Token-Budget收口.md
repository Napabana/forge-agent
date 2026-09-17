# Model-Aware Token Budget 收口

日期：2026-09-17

## 1. 本轮目标

本轮只收口 Token Budget / Semantic Packet 的模型能力语义，不重做 Context Compaction。Stage A deterministic pruning、Stage B Hybrid Structured Compaction、canonical history、deterministic evidence、Active Compacted View、checkpoint lineage 与 semantic fallback 均保持原设计。

## 2. 原问题

此前存在四个容易混淆的边界：

- `agent.budget_tokens=80000` 同时承担了 Forge 预算和模型窗口的角色；
- `TokenBudget.default_plan()` 固定拿总预算的 15% 当 output reserve；
- `cl100k_base` generic estimator 容易被误解成所有 Provider/模型的准确 tokenizer；
- Semantic summary packet 使用字符上限，不能和 Context Token Budget 使用同一单位。

`config/default.yaml` 本轮未修改；其中旧 `llm.max_tokens` / `agent.budget_tokens` 继续兼容读取，但内部语义已经拆开。

## 3. 新语义

配置层明确区分：

```text
llm.context_window             = 模型总 Context Window capability
llm.model_max_output_tokens    = 模型最大输出 capability
llm.max_output_tokens          = Forge 单次请求最大输出策略
agent.context_budget_cap       = Forge 可选 Context Window 上限
agent.context_safety_margin_tokens = 请求前安全余量
context.semantic_packet_max_tokens = Semantic Packet 独立 token 上限
```

旧字段兼容：

- `llm.max_tokens` → 仅作为 `llm.max_output_tokens` 的 legacy alias；
- `agent.budget_tokens` → 仅作为 `agent.context_budget_cap` 的 legacy alias；
- 当前保护配置里没有可靠的 `deepseek-v4.1-flash` proxy context capability，因此不硬编码、不猜测。旧 `budget_tokens: 80000` 会作为 Forge cap fallback，并在 Model-aware TokenBudget 构建时发出 warning，明确它不是模型能力。

若显式配置 `context_budget_cap: null` 且又没有可靠的 `llm.context_window`，配置加载会失败，而不是静默假装模型窗口是 80k。

CLI `--model` 覆盖模型时会使原模型的 `context_window/model_max_output_tokens` 失效；CLI 没有同步提供新 capability，所以只保留 Forge cap 并进入明确 fallback，避免把旧模型能力错误套到新模型。

## 4. Model Capability 进入 Backend

新增 `llm/capabilities.py::ModelCapabilities`。`llm/router.py` 负责把配置解析后的 capability / request policy 挂到实际 Backend：

```text
backend.model_name
backend.model_capabilities
backend.context_window
backend.model_max_output_tokens
backend.request_max_output_tokens
backend.context_budget_cap
backend.context_safety_margin_tokens
backend.semantic_packet_max_tokens
```

Provider adapter 不新增按 model name 猜能力的条件分支；OpenAI-compatible/custom proxy 仍以显式配置为最高可信来源。

为了兼容现有 CLI/Chat/API/GitHub Issue 都在传递 `config.llm.max_tokens` / `config.agent.budget_tokens` 的事实，`config/schema.py` 使用 int-compatible bridge 对象携带已归一化的元数据。对现有调用方它仍是整数，对 Router / TokenBudget 则可恢复新语义，因此没有复制四套入口配置逻辑。

## 5. TokenBudget 新公式

生产 Model-aware 路径：

```text
effective_context_window
= min(model_context_window, context_budget_cap if configured)

reserved_output_tokens
= min(request_max_output_tokens, model_max_output_tokens if known)

available_input_tokens
= effective_context_window
  - reserved_output_tokens
  - safety_margin_tokens

pressure_ratio
= projected_input_tokens / available_input_tokens
```

`projected_input_tokens` 继续覆盖 System Prompt + Tool Schema + History。Repo Map 已嵌入 System Prompt，因此只保留单独诊断值，不重复加入 projected input。

Plain `TokenBudget(total=...)` 仍保留历史 15% reserve，仅用于旧单测/程序化调用兼容；解析后的生产配置通过 model-aware bridge 进入新公式，不再使用 15%。

## 6. Token Counter

新增轻量 `TokenCounter` Protocol：

- `TiktokenModelCounter`：仅在 `tiktoken.encoding_for_model(model_name)` 能明确识别模型时使用；
- `ConservativeTokenCounter`：未知模型/自定义 OpenAI-compatible proxy 的本地 fallback，结合字符数和 UTF-8 bytes 做偏保守估算；
- `LegacyTokenCounter`：保留旧公共 helper 与既有测试兼容，不作为生产模型能力事实。

这些值全部属于 request 前的 local estimate，不声称 provider-exact，也不增加任何远程 count-tokens 网络调用。

Provider 请求完成后，`TokenUsage` 中 provider 返回的 `input_tokens/output_tokens/cached_tokens/reasoning_tokens/...` 继续作为请求后的 accounting / Trace truth；pre-request estimator 不覆盖 provider usage。

## 7. Semantic Packet 从 chars 改成 tokens

`LLMSemanticSummarizer` 使用 `semantic_packet_max_tokens`，并先计算 semantic side-call 的固定成本：

```text
semantic system prompt
+ record_context_summary tool schema
+ message framing
```

然后：

```text
packet_budget
= min(configured_semantic_packet_max_tokens,
      summarizer_available_input - fixed_request_tokens)
```

`build_semantic_packet()` 使用同一个 `TokenCounter` 装填 block，不再用 `len(text)` 作为 packet budget。

历史优先级固定为：

```text
Current Goal
→ Deterministic Evidence
→ 最近 user-authored messages
→ 更旧 user-authored messages
→ 旧 Round Summary
```

选择 user evidence 时从新到旧；选中后按原时间顺序发给 semantic model。若最新 user message 本身大于剩余预算，会保留它的 token-bounded prefix 并停止向更旧历史回退，避免旧约束挤掉新 override。自然语言分类仍完全交给 semantic summarizer，没有新增中英文关键词正则。

`context/compaction.py` 的 recent-tail token selection / retained-tail accounting / before-after accounting 也复用当前 `TokenBudget.counter`，避免 pressure 用新口径、tail 用旧口径。

## 8. 本轮验证

新增离线测试文件：

```text
tests/test_model_aware_token_budget.py
```

覆盖：

1. 同一 request 在 32k / 128k window 下产生不同 pressure；
2. output reserve 来自 request max output，不再固定 15%；
3. 128k model + 80k Forge cap → effective 80k；
4. 中文 + 英文 + code 混合 semantic packet 满足 token bound；
5. packet 紧张时 recent user override 优先于旧冲突指令；
6. pre-request estimate 不覆盖 MockBackend 的 provider-reported usage；
7. legacy `max_tokens/budget_tokens` 配置迁移语义；
8. Backend capability/request policy 元数据暴露。

当前 ChatGPT 执行容器无法解析 `github.com`，直接 `git clone` 返回 DNS 失败，因此本轮无法在该容器中运行仓库级 `pytest`，也无法读取用户本机工作区/venv。没有声称仓库 pytest 已通过。

已实际运行一个完全离线、无 Provider 的纯函数行为校验，覆盖前六个核心行为；结果均通过：

```text
32k pressure > 0.8                 PASS
128k pressure < 0.8                PASS
output reserve = request max       PASS
128k + 80k cap = 80k effective     PASS
mixed packet <= token budget       PASS
recent override retained           PASS
old conflicting instruction absent PASS
provider usage kept separate       PASS
```

建议用户本地拉取后执行：

```bash
pytest -q tests/test_model_aware_token_budget.py
pytest -q tests/test_token_budget_init.py tests/test_token_budget_improvements.py tests/test_context_compaction*.py
pytest -q
```

若有失败，应保留原始失败输出再修；不要为了通过本轮测试改 B1/B2 fixture 或历史 `evals/results`。

## 9. 已知边界

- 未知 OpenAI-compatible tokenizer 仍只是 conservative estimate，不是 provider-exact；
- 本轮不自动查询所有 Provider 的 model capability，也不维护未经验证的大模型表；
- 旧受保护配置仍只提供 80k Forge cap fallback，不能据此宣称 proxy 模型真实 context window 是 80k；
- Stage A deterministic pruner 的内部 large-output threshold/marker 逻辑未重做；本轮只统一 Context policy 的 pressure、recent-tail 与 semantic packet token 口径；
- `agent/core.py` 的 Trace token breakdown 仍是独立的本地诊断 estimate；provider usage 仍是请求后真实统计，二者不能混为一谈；
- 没有真实 Provider 调用、付费 API、B2 真实模型实验或新 benchmark。

## 10. 修改文件

- `config/schema.py`
- `llm/capabilities.py`
- `llm/router.py`
- `context/token_budget.py`
- `context/structured_compaction.py`
- `context/compaction.py`
- `tests/test_model_aware_token_budget.py`
- `AGENTS.md`
- `docs/changes/2026-09-17/Model-Aware-Token-Budget收口.md`

明确未修改：`config/default.yaml`、Repo Map P2 实现、B1/B2 fixture、`evals/results`。
