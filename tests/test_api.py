"""
tests/test_api.py

FastAPI service layer tests. They skip when optional API dependencies are not
installed, so the default CLI/dev test suite remains lightweight.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402

from entry.api import create_app  # noqa: E402
from entry.api_store import (  # noqa: E402
    STATUS_CANCELED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_health(tmp_path):
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        resp = await client.get("/health")
        assert resp.json() == {"status": "ok"}


async def test_index_points_to_api_routes(tmp_path):
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Forge Agent API"
        assert body["health"] == "/health"
        assert body["docs"] == "/docs"
        assert body["tasks"] == "/tasks"
        assert body["dashboard"] == "/dashboard"


async def test_dashboard_serves_html(tmp_path):
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        resp = await client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Forge Agent API" in resp.text


async def test_create_task_rejects_missing_repo(tmp_path):
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        resp = await client.post("/tasks", json={
            "repo_path": str(tmp_path / "missing"),
            "prompt": "fix tests",
        })
        assert resp.status_code == 400


async def test_create_task_rejects_repo_outside_allowed_roots(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    app = create_app(
        store_path=tmp_path / "api.db",
        runner=lambda *args: None,
        allowed_roots=[allowed],
    )
    async with await _client(app) as client:
        resp = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "fix tests",
        })
        assert resp.status_code == 403


async def test_queue_limit_rejects_when_full(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(
        store_path=tmp_path / "api.db",
        runner=lambda *args: None,
        queue_limit=1,
    )
    async with await _client(app) as client:
        first = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "first",
        })
        assert first.status_code == 202
        second = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "second",
        })
        assert second.status_code == 429


async def test_create_and_read_queued_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        created = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "fix tests",
        })
        assert created.status_code == 202
        task_id = created.json()["task_id"]

        loaded = await client.get(f"/tasks/{task_id}")
        assert loaded.status_code == 200
        body = loaded.json()
        assert body["id"] == task_id
        assert body["repo_path"] == str(repo.resolve())
        assert body["prompt"] == "fix tests"


async def test_list_tasks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(store_path=tmp_path / "api.db", runner=lambda *args: None)
    async with await _client(app) as client:
        await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "first",
        })
        await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "second",
        })

        resp = await client.get("/tasks?limit=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 1
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["prompt"] == "second"


async def test_events_endpoint_reads_jsonl(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    log_path = tmp_path / "events.jsonl"
    event = {
        "event_id": "e1",
        "event_type": "task_complete",
        "task_id": "forge_1",
        "timestamp": "2026-07-17T00:00:00+00:00",
        "payload": {"summary": "done"},
    }
    log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    def runner(store, task_id, request, config_path, cancel_event):
        store.mark_running(task_id)
        store.mark_finished(
            task_id,
            status=STATUS_SUCCEEDED,
            result_summary="done",
            log_path=str(log_path),
            forge_task_id="forge_1",
        )

    app = create_app(store_path=tmp_path / "api.db", runner=runner)
    async with await _client(app) as client:
        created = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "fix tests",
        })
        task_id = created.json()["task_id"]

        task = _wait_for(
            lambda: app.state.store.get_task(task_id).to_dict()
            if app.state.store.get_task(task_id).status == STATUS_SUCCEEDED
            else None
        )
        assert task["result_summary"] == "done"

        events = await client.get(f"/tasks/{task_id}/events")
        assert events.status_code == 200
        assert events.json()["events"] == [event]


async def test_cancel_running_task_sets_cancel_event(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    started = threading.Event()

    def runner(store, task_id, request, config_path, cancel_event):
        store.mark_running(task_id)
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.01)
        store.mark_canceled(task_id, result_summary="stopped")

    app = create_app(store_path=tmp_path / "api.db", runner=runner)
    async with await _client(app) as client:
        created = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "fix tests",
        })
        task_id = created.json()["task_id"]
        assert started.wait(timeout=2)

        cancel = await client.post(f"/tasks/{task_id}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["status"] in {"cancel_requested", "canceled"}

        task = _wait_for(
            lambda: app.state.store.get_task(task_id).to_dict()
            if app.state.store.get_task(task_id).status == STATUS_CANCELED
            else None
        )
        assert task["status"] == STATUS_CANCELED
        assert task["result_summary"] == "stopped"


async def test_events_stream_returns_jsonl_and_terminal_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    log_path = tmp_path / "events.jsonl"
    event = {
        "event_id": "e1",
        "event_type": "task_complete",
        "task_id": "forge_1",
        "timestamp": "2026-07-17T00:00:00+00:00",
        "payload": {"summary": "done"},
    }
    log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    def runner(store, task_id, request, config_path, cancel_event):
        assert not cancel_event.is_set()
        store.mark_running(task_id)
        store.mark_finished(
            task_id,
            status=STATUS_SUCCEEDED,
            result_summary="done",
            log_path=str(log_path),
            forge_task_id="forge_1",
        )

    app = create_app(store_path=tmp_path / "api.db", runner=runner)
    async with await _client(app) as client:
        created = await client.post("/tasks", json={
            "repo_path": str(repo),
            "prompt": "fix tests",
        })
        task_id = created.json()["task_id"]
        _wait_for(
            lambda: app.state.store.get_task(task_id)
            if app.state.store.get_task(task_id).status == STATUS_SUCCEEDED
            else None
        )

        async with client.stream("GET", f"/tasks/{task_id}/events/stream") as resp:
            text = await resp.aread()
        body = text.decode()
        assert "event: agent_event" in body
        assert "event: task_status" in body
        assert '"status": "succeeded"' in body
