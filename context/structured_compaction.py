"""Hybrid structured compaction: deterministic evidence + semantic fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from agent.task import ActionType
from context.history_evidence import (
    action_fingerprint,
    is_user_authored_message,
    iter_interactions,
)
from context.token_budget import (
    ConservativeTokenCounter,
    TokenBudget,
    TokenCounter,
    token_counter_for_model,
)
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from llm.usage import TokenUsage


_SUMMARY_TOOL_NAME = "record_context_summary"
_MAX_SEMANTIC_ITEMS = 8
_MAX_SEMANTIC_ITEM_CHARS = 500
_DEFAULT_PACKET_TOKENS = 16_000
_FRESHNESS_WARNING = (
    "Historical compacted context. Canonical Session/EventLog remain the audit source.\n"
    "Repository files, git diff/status and test results may have changed; "
    "re-read/re-run when current truth matters."
)


@dataclass(frozen=True)
class SemanticContextFields:
    hard_constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeterministicEvidence:
    unresolved_failures: tuple[str, ...]
    verification_state: tuple[str, ...]
    read_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    historical_references: tuple[str, ...]


@dataclass(frozen=True)
class StructuredContextState:
    goal: str
    semantic: SemanticContextFields
    evidence: DeterministicEvidence
    fallback_excerpts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticSummaryResult:
    fields: SemanticContextFields | None
    usage: TokenUsage
    packet_truncated: bool
    error: str | None = None


class SemanticSummarizer(Protocol):
    """Context 层只依赖这个窄接口，不依赖具体 provider SDK。"""

    def summarize(
        self,
        *,
        task_description: str,
        old_messages: list[LLMMessage],
        recent_messages: list[LLMMessage],
        evidence: DeterministicEvidence,
    ) -> SemanticSummaryResult:
        ...


class LLMSemanticSummarizer:
    """复用 Forge Tool Calling，并让 semantic side-call 服从 Token Budget。"""

    def __init__(
        self,
        backend: LLMBackend,
        *,
        max_packet_tokens: int | None = None,
    ) -> None:
        if max_packet_tokens is None:
            max_packet_tokens = int(
                getattr(backend, "semantic_packet_max_tokens", None)
                or _DEFAULT_PACKET_TOKENS
            )
        if max_packet_tokens <= 0:
            raise ValueError("max_packet_tokens must be positive")
        self.backend = backend
        self.max_packet_tokens = max_packet_tokens
        self.call_count = 0

    def summarize(
        self,
        *,
        task_description: str,
        old_messages: list[LLMMessage],
        recent_messages: list[LLMMessage],
        evidence: DeterministicEvidence,
        token_budget: TokenBudget | None = None,
    ) -> SemanticSummaryResult:
        counter = (
            token_budget.counter
            if token_budget is not None
            else token_counter_for_model(self.backend.model_name)
        )
        schema = _summary_tool_schema()
        system_prompt = _semantic_system_prompt()
        fixed_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": ""},
        ]
        fixed_tokens = counter.count_messages(fixed_messages) + counter.count_tool_schemas([schema])
        request_available = (
            token_budget.available_input_tokens
            if token_budget is not None
            else _backend_available_input_tokens(self.backend)
        )
        if request_available is None:
            # Direct/programmatic backend with no capability metadata: retain the bounded
            # packet cap, but do not claim this estimate represents provider capacity.
            request_available = fixed_tokens + self.max_packet_tokens
        packet_budget = min(self.max_packet_tokens, max(0, request_available - fixed_tokens))
        if packet_budget <= 0:
            return SemanticSummaryResult(
                fields=None,
                usage=TokenUsage(),
                packet_truncated=True,
                error="semantic request fixed sections leave no packet token budget",
            )

        packet, truncated = build_semantic_packet(
            task_description=task_description,
            old_messages=old_messages,
            recent_messages=recent_messages,
            evidence=evidence,
            max_tokens=packet_budget,
            token_counter=counter,
        )
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=packet),
        ]
        self.call_count += 1
        try:
            response = self.backend.complete(messages, [schema])
        except Exception as exc:
            return SemanticSummaryResult(
                fields=None,
                usage=TokenUsage(),
                packet_truncated=truncated,
                error=f"{type(exc).__name__}: {exc}",
            )

        fields, error = _validate_semantic_action(response.action)
        return SemanticSummaryResult(
            fields=fields,
            usage=response.usage,
            packet_truncated=truncated,
            error=error,
        )


def _backend_available_input_tokens(backend: LLMBackend) -> int | None:
    """从 Backend 的 capability/policy 元数据计算 semantic side-call 输入空间。"""
    capabilities = getattr(backend, "model_capabilities", None)
    model_window = getattr(capabilities, "context_window", None)
    context_cap = getattr(backend, "context_budget_cap", None)
    if model_window is None:
        effective_window = context_cap
    elif context_cap is None:
        effective_window = model_window
    else:
        effective_window = min(int(model_window), int(context_cap))
    if effective_window is None:
        return None

    request_max = int(getattr(backend, "request_max_output_tokens", 0) or 0)
    model_max = getattr(capabilities, "max_output_tokens", None)
    reserved_output = min(request_max, int(model_max or request_max))
    safety_margin = int(getattr(backend, "context_safety_margin_tokens", 0) or 0)
    return max(0, int(effective_window) - reserved_output - safety_margin)


def build_deterministic_evidence(
    messages: list[LLMMessage],
    *,
    old_end_index: int,
) -> DeterministicEvidence:
    """从完整 canonical history 生成不可由 semantic model 覆盖的事实证据。"""
    interactions = iter_interactions(messages)
    successful_after: dict[str, list[int]] = {}
    successful_test_after: dict[str, list[int]] = {}
    read_paths: list[str] = []
    modified_paths: list[str] = []
    verification = [
        "UNKNOWN",
        "No dedicated test/pytest observation recorded.",
        "Historical evidence only; re-run tests if current state matters.",
    ]
    latest_test_index = -1
    latest_test_ref: str | None = None

    for interaction in interactions:
        fingerprint = action_fingerprint(interaction.action)
        if interaction.observation.status == "SUCCESS":
            successful_after.setdefault(fingerprint, []).append(interaction.observation_index)
            if interaction.action.tool_name in {"test", "pytest"}:
                successful_test_after.setdefault(interaction.action.tool_name, []).append(
                    interaction.observation_index
                )
            _collect_working_set(interaction, read_paths, modified_paths)
        if (
            interaction.action.tool_name in {"test", "pytest"}
            and interaction.observation_index > latest_test_index
        ):
            latest_test_index = interaction.observation_index
            latest_test_ref = interaction.observation.event_ref
            status = "PASS" if interaction.observation.status == "SUCCESS" else "FAIL"
            detail = _compact_text(interaction.observation.body, 260) or "No detail recorded."
            verification = [
                status,
                detail,
                f"source event_ref={latest_test_ref}" if latest_test_ref else "source event_ref unavailable",
                "Historical evidence only; re-run tests if current state matters.",
            ]

    unresolved: list[str] = []
    unresolved_refs: list[str] = []
    for interaction in interactions:
        if interaction.observation_index >= old_end_index:
            continue
        if interaction.observation.status == "SUCCESS":
            continue
        superseded = False
        if interaction.action.tool_name in {"test", "pytest"}:
            superseded = any(
                index > interaction.observation_index
                for index in successful_test_after.get(interaction.action.tool_name, ())
            )
        else:
            fingerprint = action_fingerprint(interaction.action)
            superseded = any(
                index > interaction.observation_index
                for index in successful_after.get(fingerprint, ())
            )
        if superseded:
            continue
        detail = _compact_text(interaction.observation.body, 260) or "No error detail recorded."
        ref = interaction.observation.event_ref
        suffix = f" (event_ref={ref})" if ref else ""
        unresolved.append(f"{interaction.action.tool_name} ERROR: {detail}{suffix}")
        if ref:
            unresolved_refs.append(ref)

    historical_refs = _ordered_unique(
        [
            message.event_ref
            for message in messages[:old_end_index]
            if message.event_ref is not None
        ]
    )
    priority_refs = unresolved_refs + ([latest_test_ref] if latest_test_ref else [])
    refs = _ordered_unique([*priority_refs, *historical_refs])
    return DeterministicEvidence(
        unresolved_failures=tuple(_ordered_unique(unresolved)),
        verification_state=tuple(verification),
        read_paths=tuple(_ordered_unique(read_paths)),
        modified_paths=tuple(_ordered_unique(modified_paths)),
        historical_references=tuple(refs),
    )


def build_semantic_packet(
    *,
    task_description: str,
    old_messages: list[LLMMessage],
    recent_messages: list[LLMMessage],
    evidence: DeterministicEvidence,
    max_tokens: int | None = None,
    token_counter: TokenCounter | None = None,
    max_chars: int | None = None,
) -> tuple[str, bool]:
    """Build a token-bounded packet; user evidence selection is newest-first.

    ``max_chars`` is only a source-compatible alias for older callers/tests. Packing is
    always based on TokenCounter output.
    """
    _ = recent_messages
    if max_tokens is None:
        if max_chars is None:
            raise TypeError("max_tokens is required")
        max_tokens = max_chars
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    counter = token_counter or ConservativeTokenCounter()

    fixed = (
        "CURRENT GOAL (authoritative for this compaction):\n"
        f"{_compact_text(task_description, 4_000)}\n\n"
        "DETERMINISTIC EVIDENCE (do not contradict or rewrite as current truth):\n"
        f"{_render_evidence_for_packet(evidence)}\n\n"
        "Extract only natural-language constraints/decisions/progress/next actions from the historical "
        "user evidence below. Recent raw messages are intentionally excluded and remain verbatim outside "
        "this summary."
    )
    if counter.count_text(fixed) >= max_tokens:
        return _fit_text_to_tokens(fixed, max_tokens, counter), True

    remaining = max_tokens - counter.count_text(fixed + "\n\n")
    indexed_users = [
        (index, f"OLD USER MESSAGE:\n{message.content}")
        for index, message in enumerate(old_messages)
        if is_user_authored_message(message)
    ]

    selected_users: list[tuple[int, str]] = []
    for index, block in reversed(indexed_users):
        if remaining <= 0:
            break
        cost = counter.count_text(block + "\n\n")
        if cost <= remaining:
            selected_users.append((index, block))
            remaining -= cost
            continue
        # 最新 user 指令本身超预算时，保留它的 bounded prefix，而不是跳过去塞旧指令。
        if not selected_users:
            trimmed = _fit_text_to_tokens(block, remaining, counter)
            if trimmed:
                selected_users.append((index, trimmed))
                remaining -= counter.count_text(trimmed)
            break
    selected_users.sort(key=lambda item: item[0])

    round_summaries = [
        f"OLD ROUND SUMMARY:\n{message.content}"
        for message in old_messages
        if message.role == "assistant" and message.content.startswith("[Round ")
    ]
    selected_rounds: list[str] = []
    for block in reversed(round_summaries):
        cost = counter.count_text(block + "\n\n")
        if cost <= remaining:
            selected_rounds.append(block)
            remaining -= cost
    selected_rounds.reverse()

    truncated = (
        len(selected_users) != len(indexed_users)
        or len(selected_rounds) != len(round_summaries)
    )
    blocks = [block for _, block in selected_users] + selected_rounds
    parts = [fixed]
    if blocks:
        parts.append("\n\n".join(blocks))
    packet = "\n\n".join(parts)

    notice = (
        "[Some historical user-authored messages were omitted deterministically "
        "to fit the summary input token budget.]"
    )
    if truncated and counter.count_text(packet + "\n\n" + notice) <= max_tokens:
        packet = packet + "\n\n" + notice

    if counter.count_text(packet) > max_tokens:
        packet = _fit_text_to_tokens(packet, max_tokens, counter)
        truncated = True
    return packet, truncated


def _fit_text_to_tokens(text: str, token_limit: int, counter: TokenCounter) -> str:
    if token_limit <= 0:
        return ""
    if counter.count_text(text) <= token_limit:
        return text
    low = 0
    high = len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if counter.count_text(text[:midpoint]) <= token_limit:
            low = midpoint
        else:
            high = midpoint - 1
    result = text[:low]
    while result and counter.count_text(result) > token_limit:
        result = result[:-1]
    return result


def build_structured_state(
    *,
    goal: str,
    semantic: SemanticContextFields | None,
    evidence: DeterministicEvidence,
    fallback_excerpts: tuple[str, ...] = (),
) -> StructuredContextState:
    return StructuredContextState(
        goal=goal,
        semantic=semantic or SemanticContextFields(),
        evidence=evidence,
        fallback_excerpts=fallback_excerpts,
    )


def fallback_user_excerpts(messages: list[LLMMessage], *, limit: int = 6) -> tuple[str, ...]:
    """semantic 失败时保留原语言 user excerpt，但不把它们伪分类为约束。"""
    excerpts = [
        _compact_text(message.content, 320)
        for message in messages
        if is_user_authored_message(message)
    ]
    return tuple(item for item in excerpts[-limit:] if item)


def render_structured_context(
    state: StructuredContextState,
    *,
    max_chars: int = 2_000,
) -> str:
    """固定 section renderer；通过逐项降级而不是 summary[:limit] 保证结构完整。"""
    if max_chars < 900:
        raise ValueError("max_chars must be at least 900 for the fixed structured sections")

    semantic = state.semantic
    evidence = state.evidence
    groups: dict[str, list[str]] = {
        "constraints": list(semantic.hard_constraints),
        "decisions": list(semantic.decisions),
        "completed": list(semantic.completed),
        "in_progress": list(semantic.in_progress),
        "blocked": _ordered_unique([*semantic.blocked, *evidence.unresolved_failures]),
        "failures": list(evidence.unresolved_failures),
        "verification": list(evidence.verification_state),
        "read": list(evidence.read_paths),
        "modified": list(evidence.modified_paths),
        "next": list(semantic.next_actions),
        "refs": [f"event_ref={ref}" for ref in evidence.historical_references],
        "fallback": [f"Unclassified user excerpt: {item}" for item in state.fallback_excerpts],
    }
    limits = {name: len(items) for name, items in groups.items()}
    item_chars = 320
    reduction_order = [
        "refs", "fallback", "next", "decisions", "completed", "in_progress",
        "blocked", "read", "modified", "constraints", "failures", "verification",
    ]

    def render() -> str:
        return _render_with_limits(state.goal, groups, limits, item_chars)

    text = render()
    while len(text) > max_chars:
        changed = False
        for name in reduction_order:
            if limits[name] > 0:
                limits[name] -= 1
                changed = True
                break
        if not changed:
            if item_chars <= 80:
                break
            item_chars = max(80, item_chars - 40)
        text = render()

    if len(text) > max_chars:
        text = _render_with_limits(_compact_text(state.goal, 120), groups, limits, 80)
    return text[:max_chars]


def _summary_tool_schema() -> LLMToolSchema:
    array_field = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": _MAX_SEMANTIC_ITEMS,
    }
    return LLMToolSchema(
        name=_SUMMARY_TOOL_NAME,
        description=(
            "Record only evidence-supported semantic context for compaction. "
            "Do not infer current repository, git, or test state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hard_constraints": array_field,
                "decisions": array_field,
                "completed": array_field,
                "in_progress": array_field,
                "blocked": array_field,
                "next_actions": array_field,
            },
            "required": [
                "hard_constraints", "decisions", "completed",
                "in_progress", "blocked", "next_actions",
            ],
            "additionalProperties": False,
        },
    )


def _semantic_system_prompt() -> str:
    return (
        "You are Forge Agent's internal context compaction engine. "
        "Preserve the user's original language. Extract only facts explicitly supported by "
        "the supplied user-authored evidence. Later explicit user instructions override earlier ones. "
        "Do not infer current repository contents, git state, test status, file modifications, or event refs. "
        "Do not treat assistant exploratory thoughts as user constraints. "
        f"Call {_SUMMARY_TOOL_NAME} exactly once and do not call any other tool."
    )


def _validate_semantic_action(action) -> tuple[SemanticContextFields | None, str | None]:
    if action.action_type != ActionType.TOOL_CALL or action.tool_call is None:
        return None, "semantic model did not return a tool call"
    if action.tool_call.name != _SUMMARY_TOOL_NAME:
        return None, f"unexpected semantic tool: {action.tool_call.name}"
    params = action.tool_call.params
    if not isinstance(params, dict):
        return None, "semantic tool params must be an object"

    values: dict[str, tuple[str, ...]] = {}
    for key in (
        "hard_constraints", "decisions", "completed",
        "in_progress", "blocked", "next_actions",
    ):
        raw = params.get(key, [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            return None, f"semantic field {key} must be string[]"
        normalized = tuple(
            _compact_text(item, _MAX_SEMANTIC_ITEM_CHARS)
            for item in raw[:_MAX_SEMANTIC_ITEMS]
            if item.strip()
        )
        values[key] = tuple(_ordered_unique(normalized))

    return SemanticContextFields(**values), None


def _collect_working_set(interaction, read_paths: list[str], modified_paths: list[str]) -> None:
    tool = interaction.action.tool_name
    params = interaction.action.params
    if tool in {"file_read", "file_view", "git_diff", "search_text", "find_files", "find_symbol"}:
        path = params.get("path")
        if isinstance(path, str) and path.strip():
            read_paths.append(path.strip())
    if tool == "file_write":
        path = params.get("path")
        if isinstance(path, str) and path.strip():
            modified_paths.append(path.strip())
    if tool == "git_add":
        paths = params.get("paths")
        if isinstance(paths, list):
            modified_paths.extend(str(path).strip() for path in paths if str(path).strip())


def _render_evidence_for_packet(evidence: DeterministicEvidence) -> str:
    return json.dumps(
        {
            "unresolved_failures": evidence.unresolved_failures[:4],
            "verification_state": evidence.verification_state,
            "read_paths": evidence.read_paths[:12],
            "modified_paths": evidence.modified_paths[:12],
            "historical_event_ref_count": len(evidence.historical_references),
        },
        ensure_ascii=False,
        indent=2,
    )


def _render_with_limits(
    goal: str,
    groups: dict[str, list[str]],
    limits: dict[str, int],
    item_chars: int,
) -> str:
    lines = [_FRESHNESS_WARNING, "", "## Goal", f"- {_compact_text(goal, item_chars)}"]
    lines.extend(["", "## Hard Constraints"])
    lines.extend(_render_items(groups["constraints"], limits["constraints"], item_chars))
    lines.extend(["", "## Decisions"])
    lines.extend(_render_items(groups["decisions"], limits["decisions"], item_chars))
    lines.extend(["", "## Progress", "### Completed"])
    lines.extend(_render_items(groups["completed"], limits["completed"], item_chars))
    lines.append("### In Progress")
    lines.extend(_render_items(groups["in_progress"], limits["in_progress"], item_chars))
    lines.append("### Blocked")
    lines.extend(_render_items(groups["blocked"], limits["blocked"], item_chars))
    lines.extend(["", "## Unresolved Failures"])
    lines.extend(_render_items(groups["failures"], limits["failures"], item_chars))
    lines.extend(["", "## Verification State"])
    lines.extend(_render_items(groups["verification"], limits["verification"], item_chars))
    lines.extend(["", "## Working Set", "### Read / Inspect"])
    lines.extend(_render_items(groups["read"], limits["read"], item_chars))
    lines.append("### Modified / Staged")
    lines.extend(_render_items(groups["modified"], limits["modified"], item_chars))
    lines.append("- Historical working set only; re-read files/diff before relying on current contents.")
    lines.extend(["", "## Next Actions"])
    lines.extend(_render_items(groups["next"], limits["next"], item_chars))
    lines.extend(["", "## Historical References"])
    refs_and_fallback = [
        *_limited_items(groups["refs"], limits["refs"], item_chars),
        *_limited_items(groups["fallback"], limits["fallback"], item_chars),
    ]
    if refs_and_fallback:
        lines.extend(f"- {item}" for item in refs_and_fallback)
        if limits["refs"] < len(groups["refs"]) or limits["fallback"] < len(groups["fallback"]):
            lines.append("- ... additional items omitted deterministically")
    else:
        lines.append("- None identified.")
    return "\n".join(lines)


def _render_items(items: list[str], limit: int, item_chars: int) -> list[str]:
    if not items:
        return ["- None identified."]
    rendered = [f"- {item}" for item in _limited_items(items, limit, item_chars)]
    if limit < len(items):
        rendered.append("- ... additional items omitted deterministically")
    return rendered or ["- ... additional items omitted deterministically"]


def _limited_items(items: list[str], limit: int, item_chars: int) -> list[str]:
    return [_compact_text(item, item_chars) for item in items[:limit]]


def _compact_text(text: str, limit: int) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    if limit <= 3:
        return compact[:limit]
    return compact[: limit - 3] + "..."


def _ordered_unique(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item is None:
            continue
        value = str(item)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
