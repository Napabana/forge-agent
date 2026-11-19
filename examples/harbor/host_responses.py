"""Credential-free task requests backed by a trusted, budgeted host client."""
from collections.abc import Callable
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import sys
from secrets import token_urlsafe
from threading import Lock
from threading import Thread
from time import sleep, time
from typing import Any
from urllib.parse import urlsplit

from httpx import ConnectError, ConnectTimeout
from openai import APIConnectionError, RateLimitError

from llm.openai_responses import responses_input_counter
from llm.request_budget import RequestBudget, RequestBudgetExhausted

TOTAL_TOKENS = "total_tokens"
OUTPUT_ONLY = "output_only"
RATE_LIMIT_WAIT_SECONDS = 60
CONNECT_RETRY_WAIT_SECONDS = 5


def _retry_delay(error: RateLimitError) -> float:
    header = error.response.headers.get("Retry-After")
    if header is None:
        return RATE_LIMIT_WAIT_SECONDS
    try:
        seconds = float(header)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(header).timestamp() - time()
        except (TypeError, ValueError, OverflowError, OSError):
            return RATE_LIMIT_WAIT_SECONDS
    return max(1.0, seconds) if math.isfinite(seconds) else RATE_LIMIT_WAIT_SECONDS


class ProviderResponseError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__("invalid provider response")
        self.status_code = status_code


def probe_input_count(client, model: str) -> int:
    result = responses_input_counter(client)({
        "model": model, "input": "Synthetic counting probe; do not generate.",
        "tools": [{"type": "function", "name": "sample", "strict": False,
                   "parameters": {"type": "object", "properties": {}}}],
    })
    if result < 1:
        raise ValueError("positive probe input must count tokens")
    return result


def select_budget_contract(client, model: str, *, count_verified: bool) -> dict:
    try:
        count = probe_input_count(client, model)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in {401, 402, 403, 429}:
            raise RuntimeError(f"count probe blocked by provider HTTP {status}; no trials started") from None
        return {"mode": OUTPUT_ONLY, "probe": "no_count", "http_status": status,
                "error_type": type(exc).__name__}
    return {"mode": TOTAL_TOKENS if count_verified else OUTPUT_ONLY, "probe": "count_returned",
            "sample_input_tokens": count, "count_verified": count_verified}


def _local_input(items):
    if isinstance(items, str):
        return True
    if not isinstance(items, list):
        return False
    pending = None
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not all(isinstance(v, str) for v in item.values()):
            return False
        if pending is not None:
            if (item.keys() != {"type", "call_id", "output"}
                    or item["type"] != "function_call_output" or item["call_id"] != pending):
                return False
            pending = None
        elif item.keys() == {"role", "content"}:
            continue
        elif (item.keys() == {"type", "call_id", "name", "arguments"}
              and item["type"] == "function_call" and item["name"] and item["call_id"]
              and item["call_id"] not in seen):
            pending = item["call_id"]
            seen.add(pending)
        else:
            return False
    return pending is None


class HostResponses:
    """An in-process broker. The caller owns transport and trial lifecycle.

    Only a host constructs this object with upstream callables. A trial may
    supply Responses contents, never a URL, provider key, or budget policy.
    """

    def __init__(self, *, model: str, budget: RequestBudget,
                 count_input: Callable[[dict[str, Any]], int] | None,
                 send: Callable[[dict[str, Any]], dict[str, Any]], max_output: int,
                 max_requests: int, retry_rate_limit: bool = False, retry_connect: bool = False):
        if any(type(n) is not int or n < 1 for n in (max_output, max_requests)):
            raise ValueError("invalid host output cap")
        self.model = model
        self.budget = budget
        self.count_input = count_input
        self.send = send
        self.max_output = max_output
        self._remaining_requests = max_requests
        self.retry_rate_limit = retry_rate_limit
        self.retry_connect = retry_connect
        self._lock = Lock()
        self._input_tokens_observed = 0
        self._unknown_input_calls = 0
        self._rate_limit_retries = 0
        self._connect_retries = 0

    def input_usage(self) -> dict:
        with self._lock:
            return {"observed_input_tokens": self._input_tokens_observed,
                    "unknown_input_calls": self._unknown_input_calls,
                    "rate_limit_retries": self._rate_limit_retries, "connect_retries": self._connect_retries}

    def respond(self, pool: str, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {"model", "input", "tools", "tool_choice", "max_output_tokens"}
        if (not isinstance(body, dict) or body.keys() - allowed or "input" not in body
                or body.get("model") != self.model
                or pool not in {"lead", "worker", "compaction"}):
            raise ValueError("unsupported trial request")
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
        ):
            raise ValueError("only local function tools are permitted")
        if not _local_input(body["input"]):
            raise ValueError("only text and paired local tool calls are permitted")
        requested = body.get("max_output_tokens", self.max_output)
        if type(requested) is not int or requested < 1:
            raise ValueError("invalid requested output cap")
        reservation = self._reserve(pool, body, requested)
        try:
            return self._generate(body, reservation)
        except (RateLimitError, APIConnectionError) as exc:
            limited = isinstance(exc, RateLimitError)
            if limited and self.retry_rate_limit and exc.code != "insufficient_quota":
                delay, kind = _retry_delay(exc), "rate limit"
            elif not limited and self.retry_connect and isinstance(exc.__cause__, (ConnectError, ConnectTimeout)):
                delay, kind = CONNECT_RETRY_WAIT_SECONDS, "connect"
            else:
                raise
            with self._lock:
                if self._remaining_requests == 0 or delay > RATE_LIMIT_WAIT_SECONDS:
                    raise
            # The failed attempt remains charged against its reservation.
            reservation = self._reserve(pool, body, requested, delay=delay, retry_kind=kind)
            with self._lock:
                if limited:
                    self._rate_limit_retries += 1
                else:
                    self._connect_retries += 1
            return self._generate(body, reservation)

    def _reserve(self, pool, body, requested, *, delay=0, retry_kind=""):
        with self._lock:
            if self._remaining_requests == 0:
                raise RequestBudgetExhausted("trial request-attempt cap exhausted")
            self._remaining_requests -= 1
        if delay:
            print(f"Forge relay {retry_kind}: pool={pool} wait_seconds={delay:.3f} retry=1/1",
                  file=sys.stderr, flush=True)
            sleep(delay)
        return self.budget.reserve(
            pool, input_upper=self.count_input(body) if self.count_input is not None else 0,
            max_output=min(requested, self.max_output),
        )

    def _generate(self, body, reservation):
        usage = {}
        try:
            response = self.send({**body, "max_output_tokens": reservation.output_limit})
            if isinstance(response.get("usage"), dict):
                usage = response["usage"]
            return response
        finally:
            observed_input = usage.get("input_tokens")
            with self._lock:
                if type(observed_input) is int and observed_input >= 0:
                    self._input_tokens_observed += observed_input
                else:
                    self._unknown_input_calls += 1
            self.budget.settle(reservation, input_tokens=(observed_input if self.count_input is not None else 0),
                               output_tokens=usage.get("output_tokens"))


def create_host_client(*, base_url: str, api_key: str):
    """Construct a host-side SDK client with no redirect or automatic retry."""
    from openai import DefaultHttpxClient, OpenAI

    url = urlsplit(base_url)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or not api_key):
        raise ValueError("provider requires a fixed HTTPS endpoint and a host-only credential")
    return OpenAI(
        base_url=base_url, api_key=api_key, max_retries=0,
        timeout=90,
        http_client=DefaultHttpxClient(follow_redirects=False),
    )


def bind_host_client(client, *, model: str, budget: RequestBudget,
                     max_output: int, max_requests: int, contract: str = TOTAL_TOKENS,
                     retry_rate_limit: bool = False, retry_connect: bool = False) -> HostResponses:
    """Caller must verify the endpoint's accounting and own the client lifetime."""
    fixed = client.with_options(max_retries=0)
    if contract not in {TOTAL_TOKENS, OUTPUT_ONLY}:
        raise ValueError("invalid budget contract")

    def send(body):
        raw = fixed.responses.with_raw_response.create(**body)
        try:
            return raw.parse().model_dump(mode="json")
        except (AttributeError, TypeError, ValueError):
            raise ProviderResponseError(raw.status_code) from None

    return HostResponses(
        model=model, budget=budget, max_output=max_output, max_requests=max_requests,
        count_input=responses_input_counter(fixed) if contract == TOTAL_TOKENS else None,
        send=send, retry_rate_limit=retry_rate_limit, retry_connect=retry_connect,
    )


def create_confirmed_client(env):
    """Fail closed unless the operator explicitly approved this exact endpoint."""
    url = env.get("FORGE_EVAL_BASE_URL", "")
    if not url or env.get("FORGE_EVAL_VERIFIED_COUNT_URL") != url:
        raise ValueError("the provider input-count contract must be verified for this endpoint first")
    return create_host_client(base_url=url, api_key=env.get("FORGE_EVAL_API_KEY", ""))


@contextmanager
def trial_key_fd(key: str):
    """Pass a bounded secret only to the Harbor host subprocess, not Docker."""
    body = key.encode()
    if not 0 < len(body) <= 4096:
        raise ValueError("invalid trial credential size")
    with os.fdopen(os.memfd_create("forge-trial-credential", os.MFD_CLOEXEC), "w+b") as stream:
        stream.write(body)
        stream.flush()
        stream.seek(0)
        yield stream.fileno()


def create_trial_client(env):
    fd = int(env.get("FORGE_EVAL_KEY_FD", "-1"))
    if fd < 3:
        raise ValueError("paid trial requires the inherited host credential descriptor")
    # Popen(pass_fds=...) made this inheritable for Harbor only.
    os.set_inheritable(fd, False)
    with os.fdopen(fd, "rb") as stream:
        key = stream.read(4097)
    if not 0 < len(key) <= 4096:
        raise ValueError("invalid trial credential size")
    contract = env.get("FORGE_EVAL_BUDGET_CONTRACT")
    if contract == OUTPUT_ONLY:
        return create_host_client(base_url=env.get("FORGE_EVAL_BASE_URL", ""), api_key=key.decode())
    if contract not in {None, TOTAL_TOKENS}:
        raise ValueError("invalid budget contract")
    return create_confirmed_client({**env, "FORGE_EVAL_API_KEY": key.decode()})


@contextmanager
def serve_trial_broker(broker: HostResponses, *, address: str = "127.0.0.1"):
    """Serve one bounded trial on an explicit host interface, never the vendor key."""
    token = token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(15)
            super().setup()

        def log_message(self, fmt, *args):
            pass  # Do not log credentials, request text, or arguments.

        def do_POST(self):
            path = self.path.split("/")
            auth = self.headers.get("Authorization", "")
            if (len(path) != 4 or path[2:] != ["v1", "responses"]
                    or path[1] not in {"lead", "worker", "compaction"}
                    or not compare_digest(auth.encode(), f"Bearer {token}".encode())):
                self._send(403, {"error": {"message": "trial transport denied"}})
                return
            size = self.headers.get("Content-Length", "")
            if (not size.isascii() or not size.isdigit() or len(size) > 7
                    or not 0 < int(size) <= 1_000_000 or self.headers.get("Transfer-Encoding")):
                self._send(400, {"error": {"message": "invalid trial request size"}})
                return
            try:
                result = broker.respond(path[1], json.loads(self.rfile.read(int(size))))
            except Exception:
                # Provider exception messages can contain request or credential details.
                self._send(422, {"error": {"message": "trial relay rejected request"}})
                return
            self._send(200, result)

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class TrialServer(ThreadingHTTPServer):
        daemon_threads = False

    with TrialServer((address, 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port, token
        finally:
            server.shutdown()
            worker.join()
