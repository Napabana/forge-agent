import json

from agent.task import Task
from durable_runtime import DurableRuntimeStore
from durable_runtime.run_manager import DurableRun
from llm.cache_audit import RequestFingerprint, call_provider, capture_provider_requests, record_cache_audit
from llm.usage import TokenUsage
from project_memory.identity import ProjectIdentity


def test_final_request_audit_tracks_tail_and_missing_provider_cache_data(tmp_path, record_property):
    kwargs = {"model": "fixture", "input": [{"role": "system", "content": "secret prompt"},
              {"role": "user", "content": "first question"}], "tools": [{"name": "read"}]}
    first = RequestFingerprint.from_request("responses", kwargs)
    assert first == RequestFingerprint.from_request("responses", kwargs)
    with capture_provider_requests(True) as fingerprints:
        kwargs["input"][1]["content"] = "second question"
        response = call_provider("responses", lambda **request: request["model"], **kwargs)
    second = fingerprints[0]
    assert response == "fixture"
    assert first.prefix_hash == second.prefix_hash
    assert first.request_digest != second.request_digest
    with DurableRuntimeStore(ProjectIdentity("cache", "path", str(tmp_path)), tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        run.create(Task("question", str(tmp_path)), "cli")
        record_cache_audit(run, "first", 1, "provider", first, TokenUsage(input_tokens=100, cached_tokens=40))
        record_cache_audit(run, "second", 2, "provider", second,
                           TokenUsage(input_tokens=100, cache_read_reported=False))
        rows = store._conn.execute("SELECT * FROM cache_audit_events ORDER BY created_at").fetchall()
        assert rows[0]["cached_tokens"] == 40
        assert rows[1]["cached_tokens"] is None
        assert json.loads(rows[1]["changed_components_json"]) == ["tail"]
        assert "secret prompt" not in json.dumps([dict(row) for row in rows])
        record_property("prefix_divergence_correct", 1)
        record_property("cache_usage_attribution_correct", 1)


def test_openai_sdk_physical_retry_is_observed_at_local_transport(record_property):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from llm.base import LLMMessage
    from llm.openai_compat import OpenAICompatBackend

    bodies = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if len(bodies) == 1:
                status, reply = 503, {"error": {"message": "fixture retry"}}
            else:
                status, reply = 200, {
                    "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 16, "completion_tokens": 2, "total_tokens": 18},
                }
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if status == 503:
                self.send_header("Retry-After", "0.001")
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        backend = OpenAICompatBackend("fixture", "fixture-key", f"http://127.0.0.1:{server.server_port}/v1")
        try:
            with capture_provider_requests(True) as fingerprints:
                result = backend.complete([LLMMessage("user", "fixture prompt")], [])
        finally:
            backend._client.close()
            server.shutdown()
            thread.join()
    assert len(bodies) == len(fingerprints) == 2
    assert fingerprints == [RequestFingerprint.from_request("chat_completions", body) for body in bodies]
    assert result.usage.input_tokens == 16
    record_property("physical_provider_attempts", len(fingerprints))
