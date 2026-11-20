"""Fingerprint the final provider request without retaining its contents."""

import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


def _hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestFingerprint:
    protocol: str
    model: str
    request_digest: str
    prefix_hash: str
    component_hashes: dict[str, str]

    @classmethod
    def from_request(cls, protocol: str, request: dict, prompt_parts: dict | None = None) -> "RequestFingerprint":
        history = request.get("messages", request.get("input", []))
        system = [item for item in history if item.get("role") in {"system", "developer"}]
        non_system = [item for item in history if item.get("role") not in {"system", "developer"}]
        components = {
            "system": _hash([request.get("system"), request.get("instructions"), system]),
            "tools": _hash([request.get("tools"), request.get("tool_choice")]),
            "history": _hash(non_system[:-1]),
            "tail": _hash(non_system[-1:]),
            "options": _hash({k: v for k, v in request.items()
                              if k not in {"input", "messages", "system", "instructions", "tools", "tool_choice"}}),
        }
        if prompt_parts:
            components.update({name: _hash(content) for name, content in prompt_parts.items()})
        prefix = _hash([protocol, components["system"], components["tools"], components["history"]])
        return cls(protocol, str(request["model"]), _hash(request), prefix, components)


_observed: ContextVar[tuple[list[RequestFingerprint], dict] | None] = ContextVar("provider_request_audit", default=None)
_dispatch: ContextVar[object | None] = ContextVar("provider_http_dispatch", default=None)


@contextmanager
def capture_provider_requests(enabled: bool, prompt_parts: dict | None = None):
    rows = [] if enabled else None
    token = _observed.set((rows, prompt_parts or {}) if rows is not None else None)
    try:
        yield rows
    finally:
        _observed.reset(token)


def call_provider(protocol: str, create, **kwargs):
    observed = _observed.get()
    if observed is None:
        return create(**kwargs)
    rows, prompt_parts = observed
    try:
        fallback = RequestFingerprint.from_request(protocol, kwargs, prompt_parts)
    except (KeyError, TypeError, ValueError, AttributeError):
        return create(**kwargs)
    first = len(rows)
    rows.append(fallback)
    dispatched = False

    def on_request(request):
        nonlocal dispatched
        try:
            fingerprint = RequestFingerprint.from_request(protocol, json.loads(request.content), prompt_parts)
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            fingerprint = fallback
        if dispatched:
            rows.append(fingerprint)
        else:
            rows[first] = fingerprint
            dispatched = True

    token = _dispatch.set(on_request)
    try:
        return create(**kwargs)
    finally:
        _dispatch.reset(token)


def sdk_http_options(sdk) -> dict:
    """Use the SDK's own HTTP defaults with one request observation hook."""
    client = getattr(sdk, "DefaultHttpxClient", None)
    if client is None:
        return {}

    def observe(request):
        on_request = _dispatch.get()
        if on_request is not None:
            on_request(request)

    return {"http_client": client(event_hooks={"request": [observe]})}


def record_cache_audit(run, request_id: str, step: int, provider: str, fingerprint: RequestFingerprint, usage, *,
                       retention_days: int = 90, store_components: bool = True,
                       request_kind: str = "generation", attempt_index: int = 1,
                       logical_request_id: str | None = None) -> None:
    from durable_runtime.run_manager import encode
    from agent.task import EventType

    now = time.time()
    with run.transaction() as db:
        db.execute(
            """DELETE FROM cache_audit_events WHERE created_at<? AND run_id IN (
                   SELECT run_id FROM runs WHERE scope_id=?
               )""", (now - retention_days * 86400, run.owner.scope_id),
        )
        previous = db.execute(
            """SELECT a.component_hashes_json, a.created_at FROM cache_audit_events a
               JOIN runs r ON r.run_id=a.run_id WHERE r.scope_id=? AND
               a.provider=? AND a.model=? AND a.protocol=? AND a.request_kind=?
               AND a.prompt_tokens IS NOT NULL AND r.config_digest IS (
                   SELECT config_digest FROM runs WHERE run_id=?
               ) ORDER BY a.created_at DESC LIMIT 1""",
            (run.owner.scope_id, provider, fingerprint.model, fingerprint.protocol, request_kind, run.run_id),
        ).fetchone()
        prior = json.loads(previous["component_hashes_json"]) if previous else {}
        components = fingerprint.component_hashes if store_components else {}
        changed = [key for key, checksum in components.items() if prior.get(key) != checksum]
        reported = bool(usage and usage.cache_read_reported and not usage.estimated and usage.input_tokens > 0)
        db.execute(
            """INSERT INTO cache_audit_events (request_id, run_id, step_id, provider, model, protocol,
               prompt_tokens, cached_tokens, cache_write_tokens, cache_read_reported, estimated,
               component_hashes_json, changed_components_json, prefix_hash, idle_ms, created_at,
               request_kind, attempt_index, logical_request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, run.run_id, str(step), provider, fingerprint.model, fingerprint.protocol,
             usage.input_tokens if usage else None, usage.cached_tokens if reported else None,
             usage.cache_write_tokens if usage and not usage.estimated else None,
             int(reported), int(bool(usage and usage.estimated)),
             encode(components), encode(changed), fingerprint.prefix_hash,
             round((now - previous["created_at"]) * 1_000) if previous else None, now,
             request_kind, attempt_index, logical_request_id or request_id),
        )
        db.execute("UPDATE model_calls SET request_fingerprint=? WHERE model_call_id=? AND run_id=?",
                   (fingerprint.request_digest, request_id, run.run_id))
    run.trace(EventType.CACHE_AUDIT_RECORDED, step, request_id=request_id, request_kind=request_kind,
              attempt_index=attempt_index)
