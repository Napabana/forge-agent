"""
entry/api.py

FastAPI service layer for Forge Agent.

Run:
    uvicorn entry.api:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.core import AgentConfig
from agent.orchestrate import orchestrate_run
from agent.task import RunStatus, Task, infer_completion_requirements
from config.schema import load_config, merge_cli_overrides
from entry.api_store import (
    STATUS_CANCELED,
    STATUS_CANCEL_REQUESTED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    ApiTaskNotFound,
    ApiTaskStore,
)
from entry.cli import _build_registry
from llm.router import create_backend_from_config
from runtime.worktree import WorktreeResultPolicy
from task.engine import TaskEngine

logger = logging.getLogger(__name__)

Runner = Callable[[ApiTaskStore, str, dict[str, Any], str | None, threading.Event], None]


class TaskCreateRequest(BaseModel):
    repo_path: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    provider: str | None = None
    protocol: str | None = None
    model: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    sandbox: bool = False
    result_policy: WorktreeResultPolicy = WorktreeResultPolicy.KEEP_IF_CHANGED


def create_app(
    *,
    store_path: str | Path | None = None,
    runner: Runner | None = None,
    config_path: str | None = None,
    max_workers: int | None = None,
    queue_limit: int | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> FastAPI:
    """Create a FastAPI app. Tests can inject a fake runner."""

    store = ApiTaskStore(store_path or os.getenv("FORGE_API_DB", ".forge/api_tasks.db"))
    resolved_allowed_roots = _resolve_allowed_roots(allowed_roots)
    effective_queue_limit = queue_limit
    if effective_queue_limit is None:
        effective_queue_limit = int(os.getenv("FORGE_API_QUEUE_LIMIT", "50"))
    executor = ThreadPoolExecutor(
        max_workers=max_workers or int(os.getenv("FORGE_API_WORKERS", "2"))
    )
    task_runner = runner or run_agent_task
    cancel_events: dict[str, threading.Event] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        executor.shutdown(wait=False, cancel_futures=True)
        store.close()

    app = FastAPI(title="Forge Agent API", version="0.1.0", lifespan=lifespan)

    app.state.store = store
    app.state.executor = executor

    @app.get("/")
    async def index() -> dict[str, Any]:
        return {
            "name": "Forge Agent API",
            "version": "0.1.0",
            "health": "/health",
            "docs": "/docs",
            "dashboard": "/dashboard",
            "tasks": "/tasks",
            "events_stream": "/tasks/{task_id}/events/stream",
            "cancel": "/tasks/{task_id}/cancel",
            "config": {
                "queue_limit": effective_queue_limit,
                "allowed_roots": [str(p) for p in resolved_allowed_roots],
            },
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard_html()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/tasks")
    async def list_tasks(limit: int = 50) -> dict[str, Any]:
        safe_limit = min(max(limit, 1), 200)
        return {
            "tasks": [task.to_dict() for task in store.list_tasks(safe_limit)],
            "limit": safe_limit,
        }

    @app.post("/tasks", status_code=202)
    async def create_task(req: TaskCreateRequest) -> dict[str, str]:
        repo_path = Path(req.repo_path).expanduser().resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            raise HTTPException(status_code=400, detail="repo_path does not exist")
        if not _is_repo_allowed(repo_path, resolved_allowed_roots):
            raise HTTPException(status_code=403, detail="repo_path is outside allowed roots")
        if store.count_active() >= effective_queue_limit:
            raise HTTPException(status_code=429, detail="task queue is full")

        options = _model_to_dict(req)
        options["repo_path"] = str(repo_path)
        task_id = store.create_task(
            repo_path=str(repo_path),
            prompt=req.prompt,
            options=options,
        )
        cancel_event = threading.Event()
        cancel_events[task_id] = cancel_event
        executor.submit(
            _run_task_and_cleanup,
            task_runner,
            store,
            task_id,
            options,
            config_path,
            cancel_event,
            cancel_events,
        )
        return {"task_id": task_id, "status": "queued"}

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, str]:
        try:
            event = cancel_events.get(task_id)
            if event is not None:
                event.set()
            status = store.request_cancel(task_id)
            return {"task_id": task_id, "status": status}
        except ApiTaskNotFound:
            raise HTTPException(status_code=404, detail="task not found") from None

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        try:
            return store.get_task(task_id).to_dict()
        except ApiTaskNotFound:
            raise HTTPException(status_code=404, detail="task not found") from None

    @app.get("/tasks/{task_id}/events")
    async def get_task_events(task_id: str) -> dict[str, Any]:
        try:
            task = store.get_task(task_id)
        except ApiTaskNotFound:
            raise HTTPException(status_code=404, detail="task not found") from None

        if not task.log_path:
            return {"task_id": task_id, "events": []}

        log_path = Path(task.log_path)
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="event log not found")
        return {"task_id": task_id, "events": _read_jsonl(log_path)}

    @app.get("/tasks/{task_id}/events/stream")
    async def stream_task_events(task_id: str) -> StreamingResponse:
        try:
            store.get_task(task_id)
        except ApiTaskNotFound:
            raise HTTPException(status_code=404, detail="task not found") from None
        return StreamingResponse(
            _stream_task_events(store, task_id),
            media_type="text/event-stream",
        )

    return app


def _run_task_and_cleanup(
    runner: Runner,
    store: ApiTaskStore,
    task_id: str,
    request: dict[str, Any],
    config_path: str | None,
    cancel_event: threading.Event,
    cancel_events: dict[str, threading.Event],
) -> None:
    try:
        runner(store, task_id, request, config_path, cancel_event)
    finally:
        cancel_events.pop(task_id, None)


def run_agent_task(
    store: ApiTaskStore,
    api_task_id: str,
    request: dict[str, Any],
    config_path: str | None,
    cancel_event: threading.Event,
) -> None:
    """Background runner used by the HTTP API."""

    if cancel_event.is_set():
        store.mark_canceled(api_task_id, result_summary="canceled before start")
        return
    if not store.mark_running(api_task_id):
        task = store.get_task(api_task_id)
        if task.status in (STATUS_CANCELED, STATUS_CANCEL_REQUESTED):
            store.mark_canceled(api_task_id, result_summary="canceled before start")
        return

    log_path: str | None = None
    forge_task_id: str | None = None
    try:
        cfg = load_config(config_path)
        cfg = merge_cli_overrides(
            cfg,
            provider=request.get("provider"),
            protocol=request.get("protocol"),
            model=request.get("model"),
            max_steps=request.get("max_steps"),
        )
        backend = create_backend_from_config({
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "api_key": cfg.llm.api_key or None,
            "base_url": cfg.llm.base_url or None,
            "max_tokens": cfg.llm.max_tokens,
        })
        agent_cfg = AgentConfig(
            max_steps=cfg.agent.max_steps,
            budget_tokens=cfg.agent.budget_tokens,
            history_max_messages=cfg.context.history_window * 2,
            stream=False,
            cancel_event=cancel_event,
        )
        require_changes, require_tests = infer_completion_requirements(request["prompt"])
        task = Task(
            description=request["prompt"],
            repo_path=request["repo_path"],
            max_steps=cfg.agent.max_steps,
            budget_tokens=cfg.agent.budget_tokens,
            require_changes=require_changes,
            require_tests=require_tests,
        )
        engine = TaskEngine(Path(cfg.agent.log_dir) / "api_tasks.db")
        result = asyncio.run(orchestrate_run(
            backend=backend,
            task=task,
            engine=engine,
            registry_builder=_build_registry,
            log_dir=cfg.agent.log_dir,
            sandbox=bool(request.get("sandbox", False)),
            config=agent_cfg,
            confirm_callback=None,
            result_policy=request.get(
                "result_policy", WorktreeResultPolicy.KEEP_IF_CHANGED
            ),
            on_log_created=lambda tid, path: store.set_runtime_info(
                api_task_id, forge_task_id=tid, log_path=path,
            ),
        ))
        forge_task_id = result.task_id
        log_path = _find_latest_log(cfg.agent.log_dir, forge_task_id)
        if result.status == RunStatus.CANCELED or cancel_event.is_set():
            store.mark_canceled(
                api_task_id,
                result_summary=result.summary,
                log_path=log_path,
                forge_task_id=forge_task_id,
                artifact=result.worktree.to_dict() if result.worktree else None,
            )
        else:
            status = STATUS_SUCCEEDED if result.status == RunStatus.SUCCESS else STATUS_FAILED
            store.mark_finished(
                api_task_id,
                status=status,
                result_summary=result.summary,
                error=result.error,
                log_path=log_path,
                forge_task_id=forge_task_id,
                artifact=result.worktree.to_dict() if result.worktree else None,
            )
    except Exception as exc:  # noqa: BLE001 - background task must report failure
        logger.exception("API task %s failed", api_task_id)
        store.mark_finished(
            api_task_id,
            status=STATUS_FAILED,
            result_summary=None,
            error=str(exc),
            log_path=log_path,
            forge_task_id=forge_task_id,
        )


async def _stream_task_events(store: ApiTaskStore, task_id: str):
    offset = 0
    while True:
        task = store.get_task(task_id)
        log_path = Path(task.log_path) if task.log_path else None
        if log_path and log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if line:
                        yield _sse("agent_event", line)
                offset = f.tell()

        if task.status in TERMINAL_STATUSES:
            yield _sse("task_status", json.dumps(task.to_dict(), ensure_ascii=False))
            break

        yield _sse("task_status", json.dumps({
            "id": task.id,
            "status": task.status,
        }, ensure_ascii=False))
        await asyncio.sleep(1.0)


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _resolve_allowed_roots(
    allowed_roots: Iterable[str | Path] | None,
) -> list[Path]:
    raw_roots: list[str | Path]
    if allowed_roots is None:
        env_value = os.getenv("FORGE_API_ALLOWED_ROOTS", "")
        raw_roots = [p for p in env_value.split(os.pathsep) if p]
    else:
        raw_roots = list(allowed_roots)
    return [Path(root).expanduser().resolve() for root in raw_roots]


def _is_repo_allowed(repo_path: Path, allowed_roots: list[Path]) -> bool:
    if not allowed_roots:
        return True
    return any(_is_relative_to(repo_path, root) for root in allowed_roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Forge Agent API</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; color: #17202a; }
    main { max-width: 1100px; margin: 0 auto; }
    form, section { margin-bottom: 24px; }
    label { display: block; font-weight: 600; margin-top: 12px; }
    input, textarea { box-sizing: border-box; width: 100%; padding: 8px; }
    textarea { min-height: 90px; }
    button { margin-top: 12px; padding: 8px 12px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #d8dee4; padding: 8px; text-align: left; }
    code, pre { background: #f6f8fa; }
    pre { padding: 12px; overflow: auto; min-height: 120px; }
  </style>
</head>
<body>
<main>
  <h1>Forge Agent API</h1>
  <form id="task-form">
    <label>Repository path</label>
    <input id="repo" name="repo_path" placeholder="/home/jfm/example-repo" required>
    <label>Prompt</label>
    <textarea id="prompt" name="prompt" required>修复失败的 pytest</textarea>
    <button type="submit">Create task</button>
  </form>
  <section>
    <button id="refresh" type="button">Refresh tasks</button>
    <table>
      <thead><tr><th>ID</th><th>Status</th><th>Prompt</th><th>Actions</th></tr></thead>
      <tbody id="tasks"></tbody>
    </table>
  </section>
  <section>
    <h2>Events</h2>
    <pre id="events"></pre>
  </section>
</main>
<script>
async function refreshTasks() {
  const res = await fetch('/tasks');
  const data = await res.json();
  document.querySelector('#tasks').innerHTML = data.tasks.map(t => `
    <tr>
      <td><code>${t.id}</code></td>
      <td>${t.status}</td>
      <td>${t.prompt}</td>
      <td>
        <button onclick="watchTask('${t.id}')">Watch</button>
        <button onclick="cancelTask('${t.id}')">Cancel</button>
      </td>
    </tr>`).join('');
}
async function cancelTask(id) {
  await fetch(`/tasks/${id}/cancel`, {method: 'POST'});
  refreshTasks();
}
function watchTask(id) {
  const pane = document.querySelector('#events');
  pane.textContent = '';
  const source = new EventSource(`/tasks/${id}/events/stream`);
  source.addEventListener('agent_event', e => {
    pane.textContent += e.data + '\\n';
    pane.scrollTop = pane.scrollHeight;
  });
  source.addEventListener('task_status', e => {
    pane.textContent += '[status] ' + e.data + '\\n';
    pane.scrollTop = pane.scrollHeight;
    const status = JSON.parse(e.data).status;
    if (['succeeded', 'failed', 'canceled'].includes(status)) {
      source.close();
      refreshTasks();
    }
  });
}
document.querySelector('#task-form').addEventListener('submit', async e => {
  e.preventDefault();
  const payload = {
    repo_path: document.querySelector('#repo').value,
    prompt: document.querySelector('#prompt').value
  };
  await fetch('/tasks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  refreshTasks();
});
document.querySelector('#refresh').addEventListener('click', refreshTasks);
refreshTasks();
</script>
</body>
</html>"""


def _model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def _find_latest_log(log_dir: str, task_id: str) -> str | None:
    root = Path(log_dir)
    matches = list(root.glob(f"{task_id}_*.jsonl"))
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


app = create_app()
