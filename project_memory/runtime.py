"""Run-scoped memory lookup and best-effort consolidation."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from project_memory.identity import ProjectIdentityResolver
from project_memory.retrieval import MemoryRetrieval
from project_memory.store import MemoryStore, source_hashes
from project_memory.writer import MemoryWriter, now

logger = logging.getLogger(__name__)


class MemoryRuntime:
    def __init__(self, repo: str, *, root: str | None = None, budget: int = 2000, max_results: int = 5):
        self.repo = str(Path(repo).resolve())
        self.store = MemoryStore(ProjectIdentityResolver.resolve(repo), root)
        self.writer = MemoryWriter(self.store)
        self.retrieval = MemoryRetrieval(self.store, self.repo)
        self.budget, self.max_results = budget, max_results
        self.base_commit = self.commit()
        initial = self._status_files()
        hashes = source_hashes(self.repo, initial)
        self.initial_hashes = {name: hashes.get(name) for name in initial}

    def context(self, query: str, counter, log=None) -> str:
        started, span = time.perf_counter(), None
        try:
            if log is not None:
                from agent.task import EventType
                span = log.log_trace(EventType.MEMORY_RETRIEVAL_STARTED, 0)
            output, ids = self.retrieval.context(query, counter, budget=self.budget, limit=self.max_results)
            if log is not None:
                from agent.task import EventType
                log.log_trace(EventType.MEMORY_RETRIEVAL_FINISHED, 0, span_id=span,
                              memory_ids=ids, result_count=len(ids),
                              token_count=counter.count_text(output),
                              duration_ms=(time.perf_counter() - started) * 1000)
            return output
        except Exception as exc:  # noqa: BLE001 - memory is optional to a run
            logger.warning("Memory retrieval failed: %s", exc)
            if log is not None:
                try:
                    from agent.task import EventType
                    log.log_trace(EventType.MEMORY_RETRIEVAL_FAILED, 0, span_id=span,
                                  error_type=type(exc).__name__,
                                  duration_ms=(time.perf_counter() - started) * 1000)
                except Exception:
                    pass
            return ""

    def commit(self) -> str | None:
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                    capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def conclude(self, result, *, task, acceptance: str, trace_path: str | None,
                 isolate: bool = False, log=None) -> None:
        started, span = time.perf_counter(), None
        try:
            from agent.task import EventType
            if log is not None:
                span = log.log_trace(EventType.MEMORY_CONSOLIDATION_STARTED, result.steps_taken)
            changed = (list(result.worktree.changed_files) if isolate and result.worktree is not None
                       else self._changed_files())
            # Trace and diff content are deliberately excluded from the journal.
            self.writer.append_activity({"at": now(), "run_id": task.task_id,
                "summary": result.summary[:500], "status": result.status.value,
                "acceptance": acceptance, "changed_files": changed[:100],
                "trace_path": trace_path, "source_commit": self.commit(), "isolate": isolate})
            test_evidence = self._verified_test_evidence(log) if not isolate else False
            promoted = []
            for row in self.writer.pending():
                if task.task_id not in row.get("source_run_ids", []) or row.get("status") != "pending":
                    continue
                if row.get("type") not in {"feature", "experience"}:
                    continue
                if not result.is_success() or (acceptance != "passed" and not test_evidence) or isolate:
                    continue
                if (not row.get("source_files") or not set(row["source_files"]) <= set(changed)
                        or source_hashes(self.repo, row["source_files"]) != row.get("source_file_hashes")):
                    continue
                self.writer.resolve(row["candidate_id"], status="consolidated", repo=self.repo, verified=True)
                promoted.append(row["candidate_id"])
            if log is not None:
                log.log_trace(EventType.MEMORY_CONSOLIDATION_FINISHED, result.steps_taken,
                              span_id=span, candidate_ids=promoted, processed_count=len(promoted),
                              duration_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - never change successful task result
            logger.warning("Memory consolidation failed: %s", exc)
            if log is not None:
                try:
                    log.log_trace(EventType.MEMORY_CONSOLIDATION_FAILED, result.steps_taken,
                                  span_id=span, error_type=type(exc).__name__,
                                  duration_ms=(time.perf_counter() - started) * 1000)
                except Exception:
                    pass

    def record_exception(self, task, error: BaseException, *, trace_path: str | None,
                         isolate: bool = False) -> None:
        try:
            self.writer.append_activity({"at": now(), "run_id": task.task_id,
                "summary": f"{type(error).__name__}: {str(error)[:300]}", "status": "exception",
                "acceptance": "not_run", "changed_files": [], "trace_path": trace_path,
                "source_commit": self.commit(), "isolate": isolate})
        except Exception as exc:  # noqa: BLE001 - memory cannot mask the original error
            logger.warning("Memory exception journal failed: %s", exc)

    def _changed_files(self) -> list[str]:
        files = set(self._status_files())
        files = {name for name in files if name not in self.initial_hashes
                 or source_hashes(self.repo, [name]).get(name) != self.initial_hashes[name]}
        head = self.commit()
        if self.base_commit and head and self.base_commit != head:
            try:
                diff = subprocess.run(["git", "diff", "--name-only", "-z", self.base_commit, head],
                                      cwd=self.repo, capture_output=True, timeout=5, check=False)
                if diff.returncode == 0:
                    files.update(name.decode("utf-8", errors="replace") for name in diff.stdout.split(b"\0") if name)
            except (OSError, subprocess.SubprocessError):
                pass
        return sorted(files)

    @staticmethod
    def _verified_test_evidence(log) -> bool:
        if log is None:
            return False
        try:
            from agent.task import EventType
            last_change, last_test = -1, -1
            for event in log.replay():
                step = int(event.payload.get("step", event.payload.get("step_id", -1)))
                if event.event_type is EventType.REPOSITORY_EFFECT and event.payload.get("repository_content_changed"):
                    last_change = max(last_change, step)
                if (event.event_type is EventType.OBSERVATION
                        and event.payload.get("observation", {}).get("tool_name") in {"test", "pytest"}
                        and event.payload["observation"].get("status") == "success"):
                    last_test = max(last_test, step)
            return last_test >= last_change >= 0
        except (OSError, ValueError, KeyError):
            return False

    def _status_files(self) -> list[str]:
        try:
            result = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=self.repo,
                                    capture_output=True, timeout=5, check=False)
            if result.returncode:
                return []
            return [entry[3:].decode("utf-8", errors="replace") for entry in result.stdout.split(b"\0")
                    if len(entry) > 3 and entry[2:3] == b" "]
        except (OSError, subprocess.SubprocessError):
            return []
