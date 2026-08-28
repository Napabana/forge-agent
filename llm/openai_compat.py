"""
llm/openai_compat.py

OpenAI-compatible backend。覆盖：
- OpenAI (api.openai.com)
- DeepSeek (api.deepseek.com) — deepseek-chat 支持 function calling，R1 不支持
- Groq (api.groq.com)
- Ollama (localhost:11434/v1)

全部用 openai SDK，切换只改 base_url + api_key。

function calling 不支持时（如 DeepSeek R1）走文本解析 fallback：
从 LLM 输出的文本里提取 JSON 格式的 tool call。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.task import Action, ActionType, ToolCall
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema

logger = logging.getLogger(__name__)

# 不支持 function calling 的模型（前缀匹配）
_NO_FUNCTION_CALLING: tuple[str, ...] = (
    "deepseek-reasoner",    # DeepSeek R1
    "deepseek-r1",
)


class OpenAICompatBackend(LLMBackend):
    """
    OpenAI-compatible API backend。

    Args:
        model:    模型名，如 "gpt-4o", "deepseek-chat", "llama3-70b-8192"
        api_key:  API key
        base_url: API base URL，None 时用 OpenAI 官方地址
        max_tokens: 最大输出 token 数
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        self._model = model
        self._max_tokens = max_tokens
        self._use_function_calling = not any(
            model.lower().startswith(prefix) for prefix in _NO_FUNCTION_CALLING
        )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_function_calling(self) -> bool:
        return self._use_function_calling

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_messages = _to_openai_messages(messages)

        logger.debug(
            "OpenAI-compat request: model=%s messages=%d tools=%d fc=%s",
            self._model, len(api_messages), len(tools), self._use_function_calling,
        )

        if self._use_function_calling:
            response = self._complete_with_tools(api_messages, tools)
        else:
            response = self._complete_text_only(api_messages, tools)

        return response

    # ------------------------------------------------------------------
    # function calling 路径
    # ------------------------------------------------------------------

    def _complete_with_tools(
        self,
        api_messages: list[dict],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_tools = [_to_openai_tool(t) for t in tools]

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=api_messages,
            tools=api_tools,
            tool_choice="auto",
        )

        choices = getattr(response, "choices", None) or []
        input_tokens, output_tokens = _response_usage_tokens(
            response, api_messages, ""
        )
        if not choices:
            logger.warning("OpenAI-compatible response contained no choices")
            return _empty_model_response(
                "Model returned no choices",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        thought = content or "(no thought)"
        if not isinstance(thought, str):
            thought = str(thought)
        input_tokens, output_tokens = _response_usage_tokens(
            response, api_messages, "" if thought == "(no thought)" else thought
        )

        logger.debug(
            "OpenAI-compat response: finish_reason=%s input=%d output=%d",
            getattr(choice, "finish_reason", None),
            input_tokens,
            output_tokens,
        )

        action = _parse_openai_response(choice, thought)

        return LLMResponse(
            action=action,
            raw_content=thought,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ------------------------------------------------------------------
    # 文本解析 fallback（R1 等不支持 function calling 的模型）
    # ------------------------------------------------------------------

    def _complete_text_only(
        self,
        api_messages: list[dict],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        # 在 system prompt 里注入工具描述，要求模型输出 JSON
        tool_desc = _build_tool_description_for_text(tools)
        # 在第一条 system 消息后插入工具说明
        augmented = list(api_messages)
        if augmented and augmented[0]["role"] == "system":
            augmented[0] = {
                "role": "system",
                "content": augmented[0]["content"] + "\n\n" + tool_desc,
            }

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=augmented,
        )

        choices = getattr(response, "choices", None) or []
        if not choices:
            input_tokens, output_tokens = _response_usage_tokens(
                response, augmented, ""
            )
            logger.warning("OpenAI-compatible response contained no choices")
            return _empty_model_response(
                "Model returned no choices",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        raw_text = getattr(message, "content", None) if message is not None else None
        raw_text = raw_text or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)

        action = _parse_text_response(raw_text)
        input_tokens, output_tokens = _response_usage_tokens(
            response, augmented, raw_text
        )

        return LLMResponse(
            action=action,
            raw_content=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ---------------------------------------------------------------------------
# 格式转换
# ---------------------------------------------------------------------------

def _to_openai_messages(messages: list[LLMMessage]) -> list[dict]:
    """把 LLMMessage 列表转为 OpenAI messages 格式。"""
    result = []
    for msg in messages:
        if msg.tool_call_id:
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content,
            })
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


def _to_openai_tool(schema: LLMToolSchema) -> dict:
    """转换为 OpenAI tool schema 格式。"""
    return {
        "type": "function",
        "function": {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
    }


def _response_usage_tokens(
    response: Any,
    messages: list[dict],
    output_text: str,
) -> tuple[int, int]:
    """Return provider usage when present, otherwise estimate it safely."""
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
    output_tokens = (
        getattr(usage, "completion_tokens", None) if usage is not None else None
    )

    if input_tokens is None or output_tokens is None:
        from context.token_budget import estimate_tokens

        if input_tokens is None:
            input_tokens = sum(
                estimate_tokens(str(message.get("content") or ""))
                for message in messages
            )
        if output_tokens is None:
            output_tokens = estimate_tokens(output_text)

    return int(input_tokens), int(output_tokens)


def _empty_model_response(
    reason: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> LLMResponse:
    """Convert malformed-but-successful upstream responses into a safe GIVE_UP."""
    return LLMResponse(
        action=Action(
            action_type=ActionType.GIVE_UP,
            thought="",
            message=reason,
        ),
        raw_content="",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_openai_response(choice: Any, thought: str) -> Action:
    """
    解析 OpenAI API 的 choice，返回 Action。
    """
    finish_reason = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)
    tool_calls = getattr(message, "tool_calls", None) if message is not None else None

    if finish_reason == "tool_calls" and tool_calls:
        # 取第一个 tool call（agent 每轮只调一个工具）
        tc = tool_calls[0]
        function = getattr(tc, "function", None)
        if function is None:
            return Action(
                action_type=ActionType.GIVE_UP,
                thought=thought,
                message="Model returned a malformed tool call",
            )
        try:
            arguments = getattr(function, "arguments", "") or "{}"
            params = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            params = {"raw": getattr(function, "arguments", "")}

        return Action(
            action_type=ActionType.TOOL_CALL,
            thought=thought,
            tool_call=ToolCall(
                name=getattr(function, "name", "") or "unknown_tool",
                params=params,
            ),
        )

    if finish_reason == "stop":
        if thought and thought != "(no thought)":
            pseudo_call = _parse_pseudo_tool_call(thought)
            if pseudo_call is not None:
                return pseudo_call
            return Action(
                action_type=ActionType.FINISH,
                thought="",      # 普通 chat 模型没有独立推理链，thought 置空
                message=thought,  # 模型输出的内容就是最终回答
            )
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=thought,
            message="Model stopped with no content",
        )

    # length（token 超限）或其他
    return Action(
        action_type=ActionType.GIVE_UP,
        thought=thought,
        message=f"Unexpected finish_reason: {finish_reason}",
    )


# ---------------------------------------------------------------------------
# 文本解析 fallback
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_INLINE_JSON_RE = re.compile(r"\{[^{}]+\}", re.DOTALL)
_PSEUDO_TOOL_CALL_RE = re.compile(
    r"(?im)^\s*Action\s*:\s*([A-Za-z_][\w.-]*)\s*(?:\r?\n|\s+)"
    r"Params\s*:\s*",
    re.IGNORECASE | re.DOTALL,
)
_FINISH_KEYWORDS = ("task complete", "task is complete", "i have finished", "all done")
_GIVE_UP_KEYWORDS = ("cannot solve", "give up", "unable to", "i cannot")


def _parse_pseudo_tool_call(text: str) -> Action | None:
    """Recover a tool call emitted as ``Action: ...``/``Params: ...`` text.

    Some OpenAI-compatible models return a normal ``stop`` response while
    describing the intended function call in content. Treating that content as
    FINISH creates a false success, so recover it as a real tool action instead.
    """
    text = text.strip()
    match = _PSEUDO_TOOL_CALL_RE.search(text)
    if match is None:
        return None
    try:
        raw_params = text[match.end():].lstrip()
        params, _ = json.JSONDecoder().raw_decode(raw_params)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed pseudo tool call from model: %s", text[:200])
        return None
    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=text.strip(),
        tool_call=ToolCall(name=match.group(1), params=params),
    )


def _build_tool_description_for_text(tools: list[LLMToolSchema]) -> str:
    """
    给不支持 function calling 的模型注入工具描述。
    要求模型输出特定 JSON 格式：
    {"tool": "tool_name", "params": {...}}
    或者输出 FINISH / GIVE_UP 关键词。
    """
    if not tools:
        return ""

    lines = [
        "## Available tools",
        "To call a tool, output ONLY a JSON block in this exact format:",
        '```json\n{"tool": "<tool_name>", "params": {<params>}}\n```',
        "",
        "To finish the task, output: TASK_COMPLETE: <summary>",
        "To give up, output: GIVE_UP: <reason>",
        "",
        "Tools:",
    ]
    for t in tools:
        lines.append(f"- {t.name}: {t.description}")
    return "\n".join(lines)


def _parse_text_response(text: str) -> Action:
    """
    从纯文本中解析 Action。
    优先匹配 JSON block，其次匹配关键词。
    """
    text_stripped = text.strip()

    # 检查 TASK_COMPLETE
    if text_stripped.upper().startswith("TASK_COMPLETE:"):
        summary = text_stripped[len("TASK_COMPLETE:"):].strip()
        return Action(
            action_type=ActionType.FINISH,
            thought=text_stripped,
            message=summary or "Task complete",
        )

    # 检查 GIVE_UP
    if text_stripped.upper().startswith("GIVE_UP:"):
        reason = text_stripped[len("GIVE_UP:"):].strip()
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=text_stripped,
            message=reason or "Agent gave up",
        )

    # 尝试提取 JSON block（```json ... ```）
    block_match = _JSON_BLOCK_RE.search(text)
    if block_match:
        return _try_parse_tool_json(block_match.group(1), thought=text_stripped)

    # 尝试提取内联 JSON
    for m in _INLINE_JSON_RE.finditer(text):
        action = _try_parse_tool_json(m.group(0), thought=text_stripped)
        if action is not None:
            return action

    # 关键词匹配兜底
    text_lower = text.lower()
    if any(kw in text_lower for kw in _FINISH_KEYWORDS):
        return Action(
            action_type=ActionType.FINISH,
            thought=text_stripped,
            message=text_stripped,
        )
    if any(kw in text_lower for kw in _GIVE_UP_KEYWORDS):
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=text_stripped,
            message=text_stripped,
        )

    # 无法解析，GIVE_UP
    logger.warning("Could not parse action from text: %s", text_stripped[:100])
    return Action(
        action_type=ActionType.GIVE_UP,
        thought=text_stripped,
        message="Could not parse a valid action from model output",
    )


def _try_parse_tool_json(json_str: str, thought: str) -> Action | None:
    """尝试把 JSON 字符串解析为 TOOL_CALL Action，失败返回 None。"""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    tool_name = data.get("tool") or data.get("name") or data.get("function")
    params = data.get("params") or data.get("arguments") or data.get("input") or {}

    if not tool_name or not isinstance(tool_name, str):
        return None

    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=thought,
        tool_call=ToolCall(name=tool_name, params=params if isinstance(params, dict) else {}),
    )


# ---------------------------------------------------------------------------
# 流式支持
# ---------------------------------------------------------------------------

from llm.base import StreamCallback


def _openai_stream(
    self: "OpenAICompatBackend",
    messages: list,
    tools: list,
    on_text: StreamCallback | None = None,
    on_thought: StreamCallback | None = None,
) -> "LLMResponse":
    """
    OpenAI-compatible 流式调用实现。
    on_text:    最终回答（message）的流式回调
    on_thought: 推理过程（reasoning_content）的流式回调，仅推理模型有内容
    """
    api_messages = _to_openai_messages(messages)

    if self._use_function_calling:
        return _stream_with_tools(self, api_messages, tools, on_text, on_thought)
    else:
        return _stream_text_only(self, api_messages, tools, on_text)


def _stream_with_tools(self, api_messages, tools, on_text, on_thought=None):
    api_tools = [_to_openai_tool(t) for t in tools] if tools else None

    kwargs = dict(
        model=self._model,
        max_tokens=self._max_tokens,
        messages=api_messages,
        stream=True,
    )
    if api_tools:
        kwargs["tools"] = api_tools
        kwargs["tool_choice"] = "auto"

    # 收集流式 chunks
    full_text = ""
    full_reasoning = ""  # reasoning_content（推理模型专有）
    finish_reason = None
    tool_calls_raw = []      # 收集 tool call deltas

    stream = self._client.chat.completions.create(**kwargs)
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        choice = choices[0] if choices else None
        if not choice:
            continue

        finish_reason = getattr(choice, "finish_reason", None) or finish_reason
        # Some gateways emit a final choice with ``delta: null`` or put the
        # payload in ``message`` even though stream=true. Both shapes are
        # accepted at this compatibility boundary.
        delta = getattr(choice, "delta", None) or getattr(choice, "message", None)
        if delta is None:
            continue

        # reasoning_content delta（DeepSeek R1 / Claude thinking）
        reasoning_delta = (
            getattr(delta, "reasoning_content", None)
            or getattr(delta, "reasoning", None)
        )
        if reasoning_delta:
            if not isinstance(reasoning_delta, str):
                reasoning_delta = str(reasoning_delta)
            full_reasoning += reasoning_delta
            if on_thought:
                on_thought(reasoning_delta)

        # text delta（最终回答）
        content_delta = getattr(delta, "content", None)
        if content_delta:
            if not isinstance(content_delta, str):
                content_delta = str(content_delta)
            full_text += content_delta
            if on_text:
                on_text(content_delta)

        # tool call delta 拼接
        tool_call_deltas = getattr(delta, "tool_calls", None) or []
        if tool_call_deltas:
            for fallback_idx, tc_delta in enumerate(tool_call_deltas):
                if tc_delta is None:
                    continue
                idx = getattr(tc_delta, "index", None)
                if not isinstance(idx, int) or idx < 0:
                    idx = fallback_idx
                while len(tool_calls_raw) <= idx:
                    tool_calls_raw.append({"name": "", "arguments": ""})
                function = getattr(tc_delta, "function", None)
                if function is None:
                    continue
                name = getattr(function, "name", None)
                arguments = getattr(function, "arguments", None)
                if name:
                    tool_calls_raw[idx]["name"] += name
                if arguments:
                    tool_calls_raw[idx]["arguments"] += arguments

    # 构造 mock choice 供 _parse_openai_response 复用
    import json as _json
    from types import SimpleNamespace

    if tool_calls_raw:
        tcs = []
        for tc in tool_calls_raw:
            try:
                params = _json.loads(tc["arguments"])
            except Exception:
                params = {"raw": tc["arguments"]}
            fn = SimpleNamespace(name=tc["name"], arguments=tc["arguments"])
            tcs.append(SimpleNamespace(function=fn))
        mock_message = SimpleNamespace(content=full_text or None, tool_calls=tcs)
    else:
        mock_message = SimpleNamespace(content=full_text or None, tool_calls=None)

    normalized_finish_reason = "tool_calls" if tool_calls_raw else (finish_reason or "stop")
    mock_choice = SimpleNamespace(
        finish_reason=normalized_finish_reason,
        message=mock_message,
    )
    # 有 reasoning_content 时，thought = 推理过程，message = 最终回答
    # 没有时（普通 chat 模型），thought 置空，message = 模型输出
    thought_for_parse = full_text or "(no thought)"
    action = _parse_openai_response(mock_choice, thought_for_parse)
    # 如果有推理内容，覆盖 action.thought
    if full_reasoning and action.action_type.value == "finish":
        action = action.__class__(
            action_type=action.action_type,
            thought=full_reasoning,
            tool_call=action.tool_call,
            message=action.message,
        )

    # 流式模式拿不到精确 token 数，估算
    from context.token_budget import estimate_tokens
    input_tokens = sum(estimate_tokens(m.get("content", "")) for m in api_messages)
    output_tokens = estimate_tokens(full_text)

    return LLMResponse(
        action=action,
        raw_content=full_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _stream_text_only(self, api_messages, tools, on_text):
    """R1 等不支持 function calling 的模型的流式路径。"""
    tool_desc = _build_tool_description_for_text(tools)
    augmented = list(api_messages)
    if augmented and augmented[0]["role"] == "system":
        augmented[0] = {
            "role": "system",
            "content": augmented[0]["content"] + "\n\n" + tool_desc,
        }

    full_text = ""
    stream = self._client.chat.completions.create(
        model=self._model,
        max_tokens=self._max_tokens,
        messages=augmented,
        stream=True,
    )
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        choice = choices[0] if choices else None
        if not choice:
            continue
        delta = getattr(choice, "delta", None) or getattr(choice, "message", None)
        if delta is None:
            continue
        content_delta = getattr(delta, "content", None)
        if content_delta:
            if not isinstance(content_delta, str):
                content_delta = str(content_delta)
            full_text += content_delta
            if on_text:
                on_text(content_delta)

    action = _parse_text_response(full_text)

    from context.token_budget import estimate_tokens
    return LLMResponse(
        action=action,
        raw_content=full_text,
        input_tokens=sum(estimate_tokens(m.get("content", "")) for m in augmented),
        output_tokens=estimate_tokens(full_text),
    )


# 把 stream() 方法绑定到 OpenAICompatBackend
OpenAICompatBackend.stream = _openai_stream
