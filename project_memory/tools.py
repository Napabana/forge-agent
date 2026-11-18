"""Agent memory tools; proposals are Lead-only pending data."""

from __future__ import annotations

import json

from tools.base import BaseTool, ToolEffect, ToolResult


class MemorySearchTool(BaseTool):
    def __init__(self, runtime): self.runtime = runtime
    @property
    def name(self): return "memory_search"
    @property
    def description(self): return "Search historical project memory; verify matches against current source."
    @property
    def effect(self): return ToolEffect.READ_ONLY
    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}
    def execute(self, params):
        try:
            limit = params.get("limit", self.runtime.max_results)
            if not 1 <= limit <= 20:
                raise ValueError("limit must be between 1 and 20")
            rows = self.runtime.retrieval.search(params["query"], limit=limit)
            return ToolResult(True, json.dumps([{key: row.get(key) for key in ("memory_id", "title", "type", "confidence", "stale", "path", "content")} for row in rows], ensure_ascii=False))
        except (ValueError, OSError) as exc:
            return ToolResult(False, "", error=str(exc))


class MemoryReadTool(BaseTool):
    def __init__(self, runtime): self.runtime = runtime
    @property
    def name(self): return "memory_read"
    @property
    def description(self): return "Read one historical memory by ID."
    @property
    def effect(self): return ToolEffect.READ_ONLY
    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]}
    def execute(self, params):
        rows = self.runtime.store.read_records()
        row = next((row for row in rows if row["memory_id"] == params["memory_id"]), None)
        if row is None:
            return ToolResult(False, "", error="memory not found")
        from project_memory.store import source_hashes
        hashes = row.get("source_file_hashes") or {}
        row["stale"] = any(source_hashes(self.runtime.repo, [name]).get(name) != digest
                           for name, digest in hashes.items())
        if row["stale"]:
            row["status"] = "needs_revalidation"
        return ToolResult(True, json.dumps(row, ensure_ascii=False))


class MemoryProposeTool(BaseTool):
    def __init__(self, runtime, *, run_id: str, session_id: str | None = None,
                 trace_path: str | None = None, log=None, isolate: bool = False):
        self.runtime, self.run_id, self.session_id, self.trace_path = runtime, run_id, session_id, trace_path
        self.log = log
        self.isolate = isolate
    @property
    def name(self): return "memory_propose"
    @property
    def description(self): return "Propose reusable project knowledge for evidence review; creates pending data only."
    @property
    def effect(self): return ToolEffect.READ_ONLY
    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {"type": {"type": "string"}, "title": {"type": "string"},
                "content": {"type": "string"}, "related_files": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "supersedes": {"type": "array", "items": {"type": "string"}}},
                "required": ["type", "title", "content", "related_files", "tags"]}
    def execute(self, params):
        try:
            candidate_id = self.runtime.writer.propose(kind=params["type"], title=params["title"],
                content=params["content"], related_files=params["related_files"], tags=params["tags"],
                run_id=self.run_id, repo=self.runtime.repo, session_id=self.session_id,
                trace_path=self.trace_path, commit=self.runtime.commit(),
                supersedes=params.get("supersedes"), isolate=self.isolate)
            if self.log is not None:
                try:
                    from agent.task import EventType
                    self.log.log_trace(EventType.MEMORY_CANDIDATE_CREATED, 0, candidate_id=candidate_id)
                except Exception:
                    pass
            return ToolResult(True, json.dumps({"candidate_id": candidate_id, "status": "pending"}))
        except (KeyError, ValueError, OSError) as exc:
            return ToolResult(False, "", error=str(exc))
