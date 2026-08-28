"""Incremental, progress-aware action loop detection.、
重复动作、无进展和循环调用检测
这里实现的思路是：Agent 是否在重复一个动作周期，并且 Observation、代码仓库和测试状态都没有产生实际进展。
Action 是否重复；
Observation 是否变化；
仓库内容是否变化；
测试状态是否变化；
是否存在长度为 1～3 的周期。
"""

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
    """
    StepRecord
    ├── action        动作指纹
    ├── observation   工具结果指纹
    ├── repo_state    仓库状态指纹
    └── test_state    测试成功/失败状态
    """
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
        action: Action,# Agent 这一步决定做什么
        observation: Observation,# 工具执行之后返回的结果
        *,
        repo_state: str = "", # 当前代码仓库状态
        test_state: str | None = None,# 当前测试状态，可以没有
    ) -> LoopSignal | None:
        """
        记录 Agent 已经执行完的一步。
        如果发现 Agent 陷入重复循环，就返回 LoopSignal；
        如果没有发现循环，就返回 None。
        """
        
        # 1. 先判断仓库状态或测试状态有没有变化。
        # 如果变化了，说明 Agent 实际上有进展，
        # 那么之前累计的“循环出现次数”清零。
        if self._has_external_progress(repo_state, test_state):
            self._occurrence = 0

        record = _StepRecord(
            action=action_fingerprint(action),
            observation=observation_fingerprint(observation),
            repo_state=repo_state,
            test_state=test_state,
        )
        #加入队列并更新状态
        self._records.append(record)
        self._last_repo_state = repo_state
        if test_state is not None:
            self._last_test_state = test_state
        # 5. 标记：
        # 是否出现了“行为重复，但是执行结果有变化”的情况。
        progress_candidate = False
        
        # 6. 尝试检测不同长度的循环。
        #
        # max_period = 3 时：
        # period = 1：检测 A A A
        # period = 2：检测 A B A B A B
        # period = 3：检测 A B C A B C A B C        
        for period in range(1, self.max_period + 1):
            needed = period * self.repeats
            if len(self._records) < needed:
                continue
            # 只拿最近 needed 条记录。
            recent = list(self._records)[-needed:]
            # 7. 把最近记录按 period 分块。
            action_blocks = [
                tuple(item.action for item in recent[offset:offset + period])
                for offset in range(0, needed, period)
            ]
            # 8. 判断这些 action block 是否完全相同。
            if any(block != action_blocks[0] for block in action_blocks[1:]):
                continue
            # 9. 虽然 Action 重复，
            # 但还要判断每轮有没有产生新的结果。
            if self._cycles_show_progress(recent, period):
                progress_candidate = True
                continue
            # 10. 到这里说明：
                #
                # Action 重复
                # +
                # Observation / repo / test 没有进展
                #
                # 认为真正检测到了循环。
            self._occurrence += 1
            #第一次检测到循环，该让 Agent 反思
            #第二次检测到循环，直接终止循环
            severity = (
                LoopSeverity.REFLECT
                if self._occurrence == 1
                else LoopSeverity.TERMINATE
            )
            # 11. 构造循环检测结果。
            signal = LoopSignal(
                severity=severity,
                period=period,
                repeats=self.repeats,
                occurrence=self._occurrence,
                action_pattern=action_blocks[0],
            )
            # 12. 清空旧记录。
            #
            # 这样 Reflection 之后，需要重新积累一整套
            # 重复行为，才会第二次触发并升级成 TERMINATE。
            self._records.clear()
            return signal
    # 13. 如果发现“Action 虽然重复，但是实际有进展”，
    # 那之前的循环次数也不应该继续累计。
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
        """"比较：
        - 每轮 Observation 是否变化；
        - 每轮结束时仓库状态是否变化；
        - 测试状态是否变化。"""
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
    """action_fingerprint() 将 Action 的类型、工具名和参数规范化，再计算 SHA-256 摘要，从而把一次 Agent 行为表示为稳定的字符串，用于后续循环检测。
    Canonicalize action type, tool, and parameters into a stable digest."""

    payload: dict[str, Any] = {"type": action.action_type.value}
    if action.tool_call is not None:
        payload["tool"] = action.tool_call.name.strip().lower()
        payload["params"] = _normalize_value(action.tool_call.params)
        #判断message不为空
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
    """
    对字典 key 排序；
    递归处理 dict；
    递归处理 list / tuple；
    对字符串调用 _normalize_text()；
    把 \ 统一成 /。
    """
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
    """返回哈希值"""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]