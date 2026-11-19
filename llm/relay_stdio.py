"""Bounded Responses request channel over a dedicated binary stdio pipe."""
import json
from concurrent.futures import Future
from itertools import count
from threading import Lock, Thread
from types import SimpleNamespace

from llm.request_budget import RequestBudgetExhausted


MAX_FRAME = 1_000_000


def encode_frame(value: dict) -> bytes:
    frame = json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(frame) > MAX_FRAME:
        raise ValueError("trial relay frame exceeds limit")
    return frame


def read_frame(stream) -> dict | None:
    line = stream.readline(MAX_FRAME + 1)
    if not line:
        return None
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ValueError("invalid trial relay frame length")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("trial relay frame must be an object")
    return value


class ResponsesPipe:
    """One reader multiplexes the lead and workers; writes never interleave."""

    def __init__(self, stdin, stdout):
        self._input, self._output = stdin, stdout
        self._lock = Lock()
        self._wire_lock = Lock()
        self._pending: dict[int, Future] = {}
        self._ids = count(1)
        self._closed = False
        Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while (frame := read_frame(self._input)) is not None:
                with self._lock:
                    pending = self._pending.pop(frame.get("id"), None)
                if pending is None:
                    break
                if frame.get("error") == "budget":
                    pending.set_exception(RequestBudgetExhausted("trial relay budget exhausted"))
                elif "error" in frame or not isinstance(frame.get("response"), dict):
                    pending.set_exception(RuntimeError("trial relay rejected request"))
                else:
                    pending.set_result(frame["response"])
        except (OSError, ValueError, TypeError):
            pass
        finally:
            with self._lock:
                self._closed = True
                pending, self._pending = self._pending, {}
            for future in pending.values():
                future.set_exception(RuntimeError("trial relay closed"))

    def client(self, pool: str):
        return SimpleNamespace(responses=SimpleNamespace(create=lambda **body: self.request(pool, body)))

    def request(self, pool: str, body: dict) -> dict:
        future = Future()
        with self._wire_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("trial relay closed")
                request_id = next(self._ids)
                frame = encode_frame({"id": request_id, "pool": pool, "body": body})
                self._pending[request_id] = future
            try:
                self._output.write(frame)
                self._output.flush()
            except OSError:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise
        # Count and generation may each run twice, with a bounded 429 wait.
        return future.result(timeout=450)
