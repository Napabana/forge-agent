"""
llm/router.py

按 config 选择并实例化正确的 LLMBackend。
"""

from __future__ import annotations

import os

from llm.base import LLMBackend
from llm.capabilities import ModelCapabilities

_PROVIDER_BASE_URLS: dict[str, str | None] = {
    "anthropic": None,
    "openai": None,
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}

_ENV_KEY_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}


def _positive_optional(value, name: str) -> int | None:
    if value is None:
        return None
    resolved = int(value)
    if resolved <= 0:
        raise ValueError(f"{name} must be positive")
    return resolved


def _attach_capabilities(
    backend: LLMBackend,
    *,
    context_window: int | None,
    model_max_output_tokens: int | None,
    request_max_output_tokens: int,
    semantic_packet_max_tokens: int | None = None,
    context_budget_cap: int | None = None,
    context_safety_margin_tokens: int | None = None,
) -> LLMBackend:
    """把 capability / request policy 放到 Backend，而不是让 Agent 猜 model name。"""
    if model_max_output_tokens is not None and request_max_output_tokens > model_max_output_tokens:
        raise ValueError("max_output_tokens must be <= model_max_output_tokens")

    if context_window is not None and model_max_output_tokens is not None:
        source = "configured"
    elif context_window is not None or model_max_output_tokens is not None:
        source = "configured-partial"
    else:
        source = "unknown"

    capabilities = ModelCapabilities(
        context_window=context_window,
        max_output_tokens=model_max_output_tokens,
        source=source,
    )
    # Public runtime metadata：Backend 至少可提供 model name + capability + request policy。
    backend.model_capabilities = capabilities  # type: ignore[attr-defined]
    backend.context_window = context_window  # type: ignore[attr-defined]
    backend.model_max_output_tokens = model_max_output_tokens  # type: ignore[attr-defined]
    backend.request_max_output_tokens = request_max_output_tokens  # type: ignore[attr-defined]
    backend.semantic_packet_max_tokens = semantic_packet_max_tokens  # type: ignore[attr-defined]
    backend.context_budget_cap = context_budget_cap  # type: ignore[attr-defined]
    backend.context_safety_margin_tokens = context_safety_margin_tokens or 0  # type: ignore[attr-defined]
    return backend


def create_backend(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    protocol: str = "auto",
    *,
    max_output_tokens: int | None = None,
    context_window: int | None = None,
    model_max_output_tokens: int | None = None,
    semantic_packet_max_tokens: int | None = None,
    context_budget_cap: int | None = None,
    context_safety_margin_tokens: int | None = None,
) -> LLMBackend:
    """Create a backend with explicit model capability and Forge request policy.

    ``max_tokens`` remains a source-compatible alias. Parsed AppConfig supplies an
    int-compatible bridge, so current CLI/Chat/API/GitHub Issue callers need no parallel
    construction path while the backend still receives the new metadata.
    """
    provider = provider.lower().strip()
    protocol = protocol.lower().strip().replace("-", "_")

    if provider not in _PROVIDER_BASE_URLS:
        supported = ", ".join(sorted(_PROVIDER_BASE_URLS))
        raise ValueError(f"Unsupported provider '{provider}'. Supported: {supported}")

    bridge = max_tokens
    if context_window is None:
        context_window = getattr(bridge, "context_window", None)
    if model_max_output_tokens is None:
        model_max_output_tokens = getattr(bridge, "model_max_output_tokens", None)
    if semantic_packet_max_tokens is None:
        semantic_packet_max_tokens = getattr(bridge, "semantic_packet_max_tokens", None)
    if context_budget_cap is None:
        context_budget_cap = getattr(bridge, "context_budget_cap", None)
    if context_safety_margin_tokens is None:
        context_safety_margin_tokens = getattr(bridge, "context_safety_margin_tokens", None)

    context_window = _positive_optional(context_window, "context_window")
    model_max_output_tokens = _positive_optional(model_max_output_tokens, "model_max_output_tokens")
    semantic_packet_max_tokens = _positive_optional(
        semantic_packet_max_tokens, "semantic_packet_max_tokens"
    )
    context_budget_cap = _positive_optional(context_budget_cap, "context_budget_cap")
    if context_safety_margin_tokens is not None:
        context_safety_margin_tokens = int(context_safety_margin_tokens)
        if context_safety_margin_tokens < 0:
            raise ValueError("context_safety_margin_tokens cannot be negative")

    request_max_output_tokens = int(
        max_output_tokens
        if max_output_tokens is not None
        else max_tokens if max_tokens is not None else 4096
    )
    if request_max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    if model_max_output_tokens is not None and request_max_output_tokens > model_max_output_tokens:
        raise ValueError("max_output_tokens must be <= model_max_output_tokens")

    resolved_key = api_key or os.environ.get(_ENV_KEY_MAP.get(provider, ""), "")
    if not resolved_key and provider != "ollama":
        env_var = _ENV_KEY_MAP.get(provider, "")
        raise ValueError(
            f"API key for '{provider}' not provided. "
            f"Set it via config or environment variable {env_var!r}."
        )
    if not resolved_key:
        resolved_key = "ollama"

    attach_kwargs = dict(
        context_window=context_window,
        model_max_output_tokens=model_max_output_tokens,
        request_max_output_tokens=request_max_output_tokens,
        semantic_packet_max_tokens=semantic_packet_max_tokens,
        context_budget_cap=context_budget_cap,
        context_safety_margin_tokens=context_safety_margin_tokens,
    )

    if provider == "anthropic":
        if protocol not in {"auto", "anthropic", "messages"}:
            raise ValueError(f"Protocol '{protocol}' is incompatible with provider 'anthropic'")
        from llm.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend(
            model=model,
            api_key=resolved_key,
            max_tokens=request_max_output_tokens,
            base_url=base_url or None,
        )
        return _attach_capabilities(backend, **attach_kwargs)

    resolved_base_url = base_url or _PROVIDER_BASE_URLS[provider]
    if protocol in {"responses", "response"}:
        from llm.openai_responses import OpenAIResponsesBackend

        backend = OpenAIResponsesBackend(
            model=model,
            api_key=resolved_key,
            base_url=resolved_base_url,
            max_tokens=request_max_output_tokens,
        )
        return _attach_capabilities(backend, **attach_kwargs)

    if protocol not in {"auto", "chat", "chat_completion", "chat_completions"}:
        raise ValueError(
            "Unsupported LLM protocol "
            f"'{protocol}'. Supported: auto, chat_completions, responses"
        )

    from llm.openai_compat import OpenAICompatBackend

    backend = OpenAICompatBackend(
        model=model,
        api_key=resolved_key,
        base_url=resolved_base_url,
        max_tokens=request_max_output_tokens,
    )
    return _attach_capabilities(backend, **attach_kwargs)


def create_backend_from_config(config: dict) -> LLMBackend:
    """New explicit fields win; legacy max_tokens remains a compatibility alias."""
    legacy_max_tokens = config.get("max_tokens")
    max_output_tokens = config.get("max_output_tokens")
    return create_backend(
        provider=config.get("provider", "anthropic"),
        model=config.get("model", "claude-sonnet-4-5"),
        api_key=config.get("api_key") or None,
        base_url=config.get("base_url") or None,
        max_tokens=legacy_max_tokens,
        max_output_tokens=int(max_output_tokens) if max_output_tokens is not None else None,
        context_window=config.get("context_window"),
        model_max_output_tokens=config.get("model_max_output_tokens"),
        semantic_packet_max_tokens=config.get("semantic_packet_max_tokens"),
        context_budget_cap=config.get("context_budget_cap"),
        context_safety_margin_tokens=config.get("context_safety_margin_tokens"),
        protocol=config.get("protocol", "auto"),
    )
