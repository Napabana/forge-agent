"""Provider-agnostic LLM exception classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Mapping


class LLMErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    OVERLOADED = "overloaded"
    CALLBACK = "callback"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LLMErrorInfo:
    kind: LLMErrorKind
    retryable: bool
    status_code: int | None = None
    retry_after: float | None = None


class LLMCallbackError(RuntimeError):
    """A user callback failed; repeating the provider call cannot fix it."""


def classify_llm_error(exc: BaseException) -> LLMErrorInfo:
    """Classify only an explicit whitelist of transient failures as retryable."""

    if isinstance(exc, LLMCallbackError):
        return LLMErrorInfo(LLMErrorKind.CALLBACK, False)

    status_code = _status_code(exc)
    retry_after = _retry_after(exc)
    message = str(exc).lower()

    if status_code in {401, 403} or any(token in message for token in (
        "invalid api key", "authentication", "unauthorized", "permission denied",
    )):
        return LLMErrorInfo(
            LLMErrorKind.AUTHENTICATION, False, status_code, retry_after,
        )
    if status_code is not None and 400 <= status_code < 500 and status_code not in {408, 409, 429}:
        return LLMErrorInfo(LLMErrorKind.BAD_REQUEST, False, status_code, retry_after)
    if status_code == 429 or any(token in message for token in (
        "rate limit", "rate_limit", "too many requests",
    )):
        return LLMErrorInfo(LLMErrorKind.RATE_LIMIT, True, status_code, retry_after)
    if status_code in {408, 409} or isinstance(exc, TimeoutError) or any(
        token in message for token in ("timed out", "timeout")
    ):
        return LLMErrorInfo(LLMErrorKind.TIMEOUT, True, status_code, retry_after)
    if isinstance(exc, ConnectionError) or any(token in message for token in (
        "connection reset", "connection aborted", "network unreachable",
        "network timeout", "connection error",
    )):
        return LLMErrorInfo(LLMErrorKind.CONNECTION, True, status_code, retry_after)
    if status_code is not None and 500 <= status_code <= 599:
        return LLMErrorInfo(LLMErrorKind.SERVER, True, status_code, retry_after)
    if any(token in message for token in (
        "overloaded_error", "overloaded", "temporarily unavailable", "service unavailable",
    )):
        return LLMErrorInfo(LLMErrorKind.OVERLOADED, True, status_code, retry_after)
    return LLMErrorInfo(LLMErrorKind.UNKNOWN, False, status_code, retry_after)


def _status_code(exc: BaseException) -> int | None:
    for source in (exc, getattr(exc, "response", None)):
        value = getattr(source, "status_code", None)
        if value is None:
            value = getattr(source, "status", None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    value = _header_value(headers, "retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(value))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _header_value(headers: object, name: str) -> object | None:
    if headers is None:
        return None
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == name:
                return value
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter(name) or getter(name.title())
    return None