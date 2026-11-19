"""Run a credential-free container argv through the host's bounded broker."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Timer

from httpx import TransportError
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError

from examples.harbor.host_responses import ProviderResponseError
from llm.relay_stdio import encode_frame, read_frame
from llm.request_budget import RequestBudgetExhausted


def run_with_broker(command: list[str], broker, *, timeout_sec: float, console) -> int:
    """The worker command sees requests/replies, never the host SDK credential."""
    if timeout_sec <= 0:
        raise ValueError("relay timeout must be positive")
    env = {name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_CONFIG", "LANG") if name in os.environ}
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=console, env=env) as proc:
        lock = Lock()
        wire_lock = Lock()
        active = set()
        expired = False

        def expire():
            nonlocal expired
            if proc.poll() is None:
                expired = True
                proc.kill()

        def respond(frame):
            stage = "broker"
            try:
                response = broker.respond(frame["pool"], frame["body"])
                stage = "reply_encoding"
                reply = encode_frame({"id": frame["id"], "response": response})
            except RequestBudgetExhausted:
                reply = encode_frame({"id": frame["id"], "error": "budget"})
            except Exception as exc:
                if isinstance(exc, APIStatusError):
                    category = "provider_http"
                elif isinstance(exc, APITimeoutError):
                    category = "provider_timeout"
                elif isinstance(exc, APIConnectionError):
                    category = "provider_connection"
                elif isinstance(exc, (ProviderResponseError, APIResponseValidationError)):
                    category = "provider_response"
                else:
                    category = stage
                status = getattr(exc, "status_code", None)
                status = status if type(status) is int and 100 <= status <= 599 else "unknown"
                # Host stderr is captured by Harbor's trial log, not the container console.
                cause = exc.__cause__
                cause_type = type(cause).__name__ if isinstance(cause, TransportError) else "unknown"
                print(f"Forge relay failure: request={frame['id']} category={category} "
                      f"error_type={type(exc).__name__} http_status={status} transport_error={cause_type}",
                      file=sys.stderr, flush=True)
                # Never send provider exception details to the task.
                reply = encode_frame({"id": frame["id"], "error": "rejected"})
            with lock:
                active.discard(frame["id"])
            with wire_lock:
                try:
                    proc.stdin.write(reply)
                    proc.stdin.flush()
                except OSError:
                    pass

        watchdog = Timer(timeout_sec, expire)
        watchdog.start()
        last_id = 0
        try:
            with ThreadPoolExecutor(max_workers=4) as workers:
                try:
                    while (frame := read_frame(proc.stdout)) is not None:
                        seq = frame.get("id")
                        with lock:
                            if (type(seq) is not int or seq <= last_id or len(active) >= 4
                                    or frame.keys() != {"id", "pool", "body"}):
                                raise ValueError("invalid or excessive concurrent trial requests")
                            last_id = seq
                            active.add(seq)
                        workers.submit(respond, frame)
                except BaseException:
                    proc.kill()
                    raise
            if expired:
                raise TimeoutError("Docker trial relay expired")
            return proc.wait(timeout=5)
        finally:
            watchdog.cancel()
