"""Forge History 中稳定 Action/Observation grammar 的共享解析层。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from llm.base import LLMMessage


_OBSERVATION_HEADER = re.compile(
    r"^\[Tool: (?P<tool>[^|]+) \| (?P<status>SUCCESS|ERROR)\](?:\n|$)"
)
_ACTION_TOOL = re.compile(r"(?m)^Action:\s*(?P<tool>[^\s]+)\s*$")
_ACTION_PARAMS = re.compile(r"(?m)^Params:\s*(?P<params>\{.*\})\s*$")
_REFLECTION_PREFIX = "[REFLECTION]"


@dataclass(frozen=True)
class ParsedAction:
    """从 Forge assistant history message 中解析出的确定性工具调用。"""

    tool_name: str
    params: dict[str, Any]
    event_ref: str | None
    raw_content: str


@dataclass(frozen=True)
class ParsedObservation:
    """从 Forge user history message 中解析出的确定性 Observation。"""

    tool_name: str
    status: str
    body: str
    event_ref: str | None
    raw_content: str


@dataclass(frozen=True)
class HistoryInteraction:
    """一组相邻且工具名一致的 Action/Observation 证据。"""

    action_index: int
    observation_index: int
    action: ParsedAction
    observation: ParsedObservation


def parse_action_message(message: LLMMessage) -> ParsedAction | None:
    """解析 Core 生成的 ``Action:`` / ``Params:`` 文本；非协议消息返回 None。"""
    if message.role != "assistant":
        return None
    tool_match = _ACTION_TOOL.search(message.content)
    if tool_match is None:
        return None
    params: dict[str, Any] = {}
    params_match = _ACTION_PARAMS.search(message.content)
    if params_match is not None:
        try:
            raw_params = json.loads(params_match.group("params"))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(raw_params, dict):
            return None
        params = raw_params
    return ParsedAction(
        tool_name=tool_match.group("tool").strip(),
        params=params,
        event_ref=message.event_ref,
        raw_content=message.content,
    )


def parse_observation_message(message: LLMMessage) -> ParsedObservation | None:
    """解析 Core 生成的 ``[Tool: ... | SUCCESS/ERROR]`` Observation。"""
    if message.role not in {"user", "tool"}:
        return None
    match = _OBSERVATION_HEADER.match(message.content)
    if match is None:
        return None
    return ParsedObservation(
        tool_name=match.group("tool").strip(),
        status=match.group("status"),
        body=message.content[match.end():],
        event_ref=message.event_ref,
        raw_content=message.content,
    )


def iter_interactions(messages: Iterable[LLMMessage]) -> tuple[HistoryInteraction, ...]:
    """按 canonical 顺序返回工具名一致的相邻 Action/Observation pair。"""
    items = list(messages)
    interactions: list[HistoryInteraction] = []
    for index in range(len(items) - 1):
        action = parse_action_message(items[index])
        if action is None:
            continue
        observation = parse_observation_message(items[index + 1])
        if observation is None or observation.tool_name != action.tool_name:
            continue
        interactions.append(HistoryInteraction(
            action_index=index,
            observation_index=index + 1,
            action=action,
            observation=observation,
        ))
    return tuple(interactions)


def action_fingerprint(action: ParsedAction) -> str:
    """只基于 tool + canonical JSON params 生成稳定 fingerprint，不包含 Thought。"""
    payload = json.dumps(
        {"tool": action.tool_name, "params": action.params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_user_authored_message(message: LLMMessage) -> bool:
    """排除 Tool Observation 与 Forge 自动 Reflection，只保留用户自然语言消息。"""
    if message.role != "user" or parse_observation_message(message) is not None:
        return False
    return not message.content.lstrip().startswith(_REFLECTION_PREFIX)
