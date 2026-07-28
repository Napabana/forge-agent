"""Incremental, progress-aware action loop detection."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agent.task import Action, Observation


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-Z]+\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|sec|seconds)\b", re.IGNORECASE)
_IGNORED_STATE_DIRS = {".git", ".pytest_cache", "__pycache__", "logs"}


class LoopSeverity(str, Enum):
    REFLECT = "reflect"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class LoopSignal:
    severity: LoopSeverity
    period: int
    repeats: int
    occurrence: int
    action_pattern: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "period": self.period,
            "repeats": self.repeats,
            "occurrence": self.occurrence,
            "action_pattern": list(self.action_pattern),
            "progress": False,
        }


@dataclass(frozen=True)
class _StepRecord:
    action: str
    observation: str
    repo_state: str
    test_state: str | None


class LoopDetector:
    """Detect repeated periods from a bounded in-memory queue."""

    def __init__(
        self,
        *,
        repeats: int = 3,
        max_period: int = 3,
        capacity: int | None = None,
    ) -> None:
        self.repeats = max(2, repeats)
        self.max_period = max(1, max_period)
        minimum_capacity = self.repeats * self.max_period
        self._records: deque[_StepRecord] = deque(
            maxlen=max(minimum_capacity, capacity or minimum_capacity),
        )
        self._occurrence = 0
        self._last_repo_state: str | None = None
        self._last_test_state: str | None = None

    @property
    def buffered_steps(self) -> int:
        return len(self._records)

    def observe(
        self,
        action: Action,
        observation: Observation,
        *,
        repo_state: str = "",
        test_state: str | None = None,
    ) -> LoopSignal | None:
        """Add one completed tool step and return a recovery/termination signal."""

        if self._has_external_progress(repo_state, test_state):
            self._occurrence = 0

        record = _StepRecord(
            action=action_fingerprint(action),
            observation=observation_fingerprint(observation),
            repo_state=repo_state,
            test_state=test_state,
        )
        self._records.append(record)
        self._last_repo_state = repo_state
        if test_state is not None:
            self._last_test_state = test_state

        progress_candidate = False
        for period in range(1, self.max_period + 1):
            needed = period * self.repeats
            if len(self._records) < needed:
                continue
            recent = list(self._records)[-needed:]
            action_blocks = [
                tuple(item.action for item in recent[offset:offset + period])
                for offset in range(0, needed, period)
            ]
            if any(block != action_blocks[0] for block in action_blocks[1:]):
                continue
            if self._cycles_show_progress(recent, period):
                progress_candidate = True
                continue

            self._occurrence += 1
            severity = (
                LoopSeverity.REFLECT
                if self._occurrence == 1
                else LoopSeverity.TERMINATE
            )
            signal = LoopSignal(
                severity=severity,
                period=period,
                repeats=self.repeats,
                occurrence=self._occurrence,
                action_pattern=action_blocks[0],
            )
            # Require a fresh set of repeated cycles before escalating again.
            self._records.clear()
            return signal

        if progress_candidate:
            self._occurrence = 0
        return None

    def _has_external_progress(self, repo_state: str, test_state: str | None) -> bool:
        repo_changed = (
            self._last_repo_state is not None
            and repo_state != self._last_repo_state
        )
        test_changed = (
            test_state is not None
            and self._last_test_state is not None
            and test_state != self._last_test_state
        )
        return repo_changed or test_changed

    def _cycles_show_progress(self, records: list[_StepRecord], period: int) -> bool:
        cycles = [
            records[offset:offset + period]
            for offset in range(0, len(records), period)
        ]
        observation_blocks = [
            tuple(item.observation for item in cycle)
            for cycle in cycles
        ]
        if len(set(observation_blocks)) > 1:
            return True
        repo_end_states = {cycle[-1].repo_state for cycle in cycles}
        if len(repo_end_states) > 1:
            return True
        test_end_states = {cycle[-1].test_state for cycle in cycles}
        return len(test_end_states) > 1


def action_fingerprint(action: Action) -> str:
    """Canonicalize action type, tool, and parameters into a stable digest."""

    payload: dict[str, Any] = {"type": action.action_type.value}
    if action.tool_call is not None:
        payload["tool"] = action.tool_call.name.strip().lower()
        payload["params"] = _normalize_value(action.tool_call.params)
    elif action.message:
        payload["message"] = _normalize_text(action.message)
    return _digest(payload)


def observation_fingerprint(observation: Observation) -> str:
    payload = {
        "tool": observation.tool_name.strip().lower(),
        "status": observation.status.value,
        "output": _normalize_text(observation.output),
        "error": _normalize_text(observation.error or ""),
    }
    return _digest(payload)


def snapshot_repository(repo_path: str | Path) -> str:
    """Return a stable working-tree fingerprint without mutating the repository."""

    root = Path(repo_path)
    if not root.is_dir():
        return "missing"
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if status.returncode == 0:
            relevant = []
            for line in status.stdout.splitlines():
                relative = line[3:].strip().strip('"').replace("\\", "/")
                current_path = relative.rsplit(" -> ", 1)[-1]
                if current_path.split("/", 1)[0] in _IGNORED_STATE_DIRS:
                    continue
                content_hash = "missing"
                candidate = root / current_path
                if candidate.is_file():
                    try:
                        content_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
                    except OSError:
                        content_hash = "unavailable"
                relevant.append((line, content_hash))
            return _digest(relevant)
    except (OSError, subprocess.SubprocessError):
        pass

    state = []
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in _IGNORED_STATE_DIRS for part in relative.parts):
                continue
            if not path.is_file():
                continue
            stat = path.stat()
            state.append((str(relative), stat.st_size, stat.st_mtime_ns))
    except OSError:
        return "unavailable"
    return _digest(state)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str):
        return _normalize_text(value).replace("\\", "/")
    return value


def _normalize_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    text = _DURATION_RE.sub("<duration>", text)
    return " ".join(text.split())


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]