"""Trace v2 schema constants and schema-level redaction helpers.

This module deliberately has no dependency on EventLog or provider SDKs so the
same sanitisation rules can be applied at the final JSONL boundary and reused by
tests/reporting code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

TRACE_SCHEMA_VERSION = 2
REDACTED = "[REDACTED]"

# Exact credential-bearing field names. Key matching is case-insensitive and
# treats '-' and '_' equivalently.
_SENSITIVE_KEYS = frozenset({
    "authorization",
    "proxy_authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "github_token",
    "password",
    "secret",
    "token",
})

# Token accounting fields are telemetry, not credentials. Keep these readable
# even though their names contain the word "token".
_SAFE_TOKEN_KEYS = frozenset({
    "input_token",
    "input_tokens",
    "output_token",
    "output_tokens",
    "cached_token",
    "cached_tokens",
    "cache_write_token",
    "cache_write_tokens",
    "reasoning_token",
    "reasoning_tokens",
    "total_token",
    "total_tokens",
    "system_token",
    "system_tokens",
    "tool_schema_token",
    "tool_schema_tokens",
    "repo_map_token",
    "repo_map_tokens",
    "history_token",
    "history_tokens",
    "context_token",
    "context_tokens",
    "pending_token",
    "pending_tokens",
    "injected_token",
    "injected_tokens",
    "estimated_input_token",
    "estimated_input_tokens",
    "before_token",
    "before_tokens",
    "after_token",
    "after_tokens",
    "budget_token",
    "budget_tokens",
    "max_token",
    "max_tokens",
    "token_breakdown",
    "tokens_used",
})

_BEARER_RE = re.compile(
    r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{6,})"
)
_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")
_GHP_RE = re.compile(r"(?i)\bghp_[A-Za-z0-9]{8,}\b")
_GITHUB_PAT_RE = re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{8,}\b")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|api[_-]?key|apikey|"
    r"access[_-]?token|refresh[_-]?token|github[_-]?token|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _normalize_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def is_sensitive_trace_key(key: object) -> bool:
    """Return whether a mapping key conventionally contains a credential.

    The rule is intentionally field-aware instead of deleting everything whose
    name contains ``token``; usage counters such as ``input_tokens`` remain
    intact.
    """

    normalized = _normalize_key(key)
    if normalized in _SAFE_TOKEN_KEYS:
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(("_password", "_secret", "_token", "_api_key"))


def redact_trace_string(value: str) -> str:
    """Redact common credential patterns embedded in free-form text/errors."""

    redacted = _BEARER_RE.sub("Bearer " + REDACTED, value)
    redacted = _SK_RE.sub("sk-" + REDACTED, redacted)
    redacted = _GHP_RE.sub("ghp_" + REDACTED, redacted)
    redacted = _GITHUB_PAT_RE.sub("github_pat_" + REDACTED, redacted)
    redacted = _INLINE_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        redacted,
    )
    return redacted


def redact_trace_value(value: Any) -> Any:
    """Recursively redact trace payloads while preserving their JSON shape."""

    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_trace_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact_trace_value(item)
        return result
    if isinstance(value, list):
        return [redact_trace_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_trace_value(item) for item in value)
    if isinstance(value, set):
        return [redact_trace_value(item) for item in value]
    if isinstance(value, str):
        return redact_trace_string(value)
    return value


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "REDACTED",
    "is_sensitive_trace_key",
    "redact_trace_string",
    "redact_trace_value",
]
