from __future__ import annotations

import builtins

import context.token_budget as token_budget


def test_failed_tiktoken_initialization_is_cached(monkeypatch) -> None:
    """tiktoken 不可用时只尝试一次 import，后续直接使用 fallback 估算。"""
    real_import = builtins.__import__
    attempts = 0

    def fake_import(name, *args, **kwargs):
        nonlocal attempts
        if name == "tiktoken":
            attempts += 1
            raise ImportError("forced tiktoken import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(token_budget, "_tiktoken_enc", None)
    monkeypatch.setattr(token_budget, "_tiktoken_available", False)
    monkeypatch.setattr(token_budget, "_tiktoken_initialized", False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert token_budget.estimate_tokens("abcdefgh") == 2
    assert token_budget.estimate_tokens("abcd") == 1
    assert token_budget.is_tiktoken_available() is False
    assert attempts == 1
    assert token_budget._tiktoken_initialized is True
