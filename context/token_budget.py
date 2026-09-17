"""Token budgeting for prompt sections and conversation history."""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass
from typing import Protocol


_tiktoken_enc = None
_tiktoken_available = False
_tiktoken_initialized = False
_MESSAGE_PROTOCOL_TOKENS = 4
_TRUNCATION_SUFFIX = "\n... [tokens truncated]"
_LEGACY_DEFAULT_TOKEN_BUDGET = 80_000


def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available, _tiktoken_initialized
    if _tiktoken_initialized:
        return
    _tiktoken_initialized = True
    try:
        import tiktoken

        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        _tiktoken_enc = None
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """Legacy generic estimate retained for compatibility; not provider-exact."""
    if not _tiktoken_initialized:
        _init_tiktoken()
    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass
    return max(1, (len(text) + 3) // 4)


def estimate_chars(tokens: int) -> int:
    return tokens * 4


def is_tiktoken_available() -> bool:
    _init_tiktoken()
    return _tiktoken_available


def estimate_message_tokens(message: dict) -> int:
    total = _MESSAGE_PROTOCOL_TOKENS
    total += estimate_tokens(str(message.get("role", "")))
    total += estimate_tokens(str(message.get("content", "")))
    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if value:
            total += estimate_tokens(str(value)) + 1
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def _tool_payload(tools) -> list[dict]:
    return [
        {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "parameters": getattr(tool, "parameters", {}),
        }
        for tool in tools
    ]


def estimate_tool_schemas_tokens(tools) -> int:
    payload = _tool_payload(tools)
    if not payload:
        return 0
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, sort_keys=True))


class TokenCounter(Protocol):
    """Local pre-request estimator. It is never provider billing truth."""

    name: str
    estimated: bool

    def count_text(self, text: str) -> int:
        ...

    def count_message(self, message: dict) -> int:
        ...

    def count_messages(self, messages: list[dict]) -> int:
        ...

    def count_tool_schemas(self, tools) -> int:
        ...


class LegacyTokenCounter:
    """Compatibility counter backed by the historical module-level helpers."""

    name = "legacy-generic"
    estimated = True

    def count_text(self, text: str) -> int:
        return estimate_tokens(text)

    def count_message(self, message: dict) -> int:
        return estimate_message_tokens(message)

    def count_messages(self, messages: list[dict]) -> int:
        return estimate_messages_tokens(messages)

    def count_tool_schemas(self, tools) -> int:
        return estimate_tool_schemas_tokens(tools)


class ConservativeTokenCounter:
    """Tokenizer-agnostic local fallback biased upward for UTF-8-heavy text."""

    name = "conservative-local-estimate"
    estimated = True

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        by_chars = math.ceil(len(text) / 4)
        by_bytes = math.ceil(len(text.encode("utf-8")) / 3)
        return max(1, by_chars, by_bytes)

    def count_message(self, message: dict) -> int:
        total = _MESSAGE_PROTOCOL_TOKENS
        total += self.count_text(str(message.get("role", "")))
        total += self.count_text(str(message.get("content", "")))
        for key in ("name", "tool_call_id"):
            value = message.get(key)
            if value:
                total += self.count_text(str(value)) + 1
        return total

    def count_messages(self, messages: list[dict]) -> int:
        return sum(self.count_message(message) for message in messages)

    def count_tool_schemas(self, tools) -> int:
        payload = _tool_payload(tools)
        if not payload:
            return 0
        return self.count_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


class TiktokenModelCounter(ConservativeTokenCounter):
    """Local tiktoken counter for a model explicitly recognized by tiktoken."""

    estimated = True

    def __init__(self, model_name: str, encoding) -> None:
        self.model_name = model_name
        self.encoding = encoding
        self.name = f"tiktoken:{getattr(encoding, 'name', 'model')}"

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        try:
            return max(1, len(self.encoding.encode(text)))
        except Exception:
            return super().count_text(text)


def token_counter_for_model(model_name: str | None) -> TokenCounter:
    """Use a locally known model tokenizer; otherwise use a conservative estimate."""
    if model_name:
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(model_name)
            return TiktokenModelCounter(model_name, encoding)
        except Exception:
            pass
    return ConservativeTokenCounter()


@dataclass
class BudgetPlan:
    total: int
    system_core: int
    repo_map: int
    history: int
    observation: int
    reserve: int
    reserved_output_tokens: int = 0
    safety_margin_tokens: int = 0
    legacy_reserve_tokens: int = 0

    @property
    def available(self) -> int:
        return self.total - self.reserve


@dataclass(frozen=True)
class ContextPressure:
    total: int
    reserve: int
    available_input: int
    system_tokens: int
    tool_schema_tokens: int
    repo_map_tokens: int
    history_tokens: int
    projected_input: int
    reserved_output_tokens: int = 0
    safety_margin_tokens: int = 0
    counter_name: str = "legacy-generic"
    capability_fallback: bool = False

    @property
    def ratio(self) -> float:
        if self.available_input <= 0:
            return 1.0
        return self.projected_input / self.available_input


@dataclass(frozen=True)
class HistoryUnit:
    indices: tuple[int, ...]

    @property
    def message_count(self) -> int:
        return len(self.indices)

    @property
    def recency(self) -> int:
        return sum(self.indices)


_HistoryUnit = HistoryUnit


def history_units(messages: list[dict], start_index: int = 1) -> list[HistoryUnit]:
    units: list[HistoryUnit] = []
    index = max(0, start_index)
    while index < len(messages):
        role = messages[index].get("role")
        if (
            role == "assistant"
            and index + 1 < len(messages)
            and messages[index + 1].get("role") in {"user", "tool"}
        ):
            units.append(HistoryUnit((index, index + 1)))
            index += 2
        else:
            units.append(HistoryUnit((index,)))
            index += 1
    return units


def history_unit_tokens(
    messages: list[dict],
    unit: HistoryUnit,
    *,
    counter: TokenCounter | None = None,
) -> int:
    active = counter or LegacyTokenCounter()
    return sum(active.count_message(messages[index]) for index in unit.indices)


def recent_history_units(
    messages: list[dict],
    token_limit: int,
    *,
    start_index: int = 1,
    counter: TokenCounter | None = None,
) -> tuple[HistoryUnit, ...]:
    if token_limit <= 0:
        return ()

    active = counter or LegacyTokenCounter()
    selected: list[HistoryUnit] = []
    used = 0
    for unit in reversed(history_units(messages, start_index=start_index)):
        cost = history_unit_tokens(messages, unit, counter=active)
        if selected and used + cost > token_limit:
            break
        selected.append(unit)
        used += cost
        if used >= token_limit:
            break
    return tuple(reversed(selected))


class TokenBudget:
    """Pre-request budget derived from model capability and Forge request policy.

    Parsed production config carries model-aware metadata through the legacy integer
    field so existing entrypoints stay source-compatible. Plain ``TokenBudget(total=...)``
    remains a legacy programmatic constructor and retains the historical 15% reserve;
    the production model-aware path does not use that percentage.
    """

    def __init__(
        self,
        total: int | None = None,
        *,
        model_context_window: int | None = None,
        model_max_output_tokens: int | None = None,
        request_max_output_tokens: int | None = None,
        context_budget_cap: int | None = None,
        safety_margin_tokens: int = 0,
        token_counter: TokenCounter | None = None,
        capability_fallback: bool = False,
    ) -> None:
        bridge = total
        bridge_is_model_aware = hasattr(bridge, "request_max_output_tokens")
        if bridge_is_model_aware:
            if model_context_window is None:
                model_context_window = getattr(bridge, "model_context_window", None)
            if model_max_output_tokens is None:
                model_max_output_tokens = getattr(bridge, "model_max_output_tokens", None)
            if request_max_output_tokens is None:
                request_max_output_tokens = int(getattr(bridge, "request_max_output_tokens"))
            if context_budget_cap is None:
                context_budget_cap = getattr(bridge, "context_budget_cap", None)
            safety_margin_tokens = int(
                getattr(bridge, "safety_margin_tokens", safety_margin_tokens)
            )
            capability_fallback = bool(
                getattr(bridge, "capability_fallback", capability_fallback)
            )
            if token_counter is None:
                token_counter = token_counter_for_model(getattr(bridge, "model_name", None))

        legacy_only = (
            not bridge_is_model_aware
            and model_context_window is None
            and context_budget_cap is None
            and request_max_output_tokens is None
            and model_max_output_tokens is None
        )
        legacy_reserve = 0
        if legacy_only:
            effective = int(total if total is not None else _LEGACY_DEFAULT_TOKEN_BUDGET)
            reserved_output = 0
            safety_margin_tokens = 0
            legacy_reserve = int(effective * 0.15)
            capability_fallback = True
        else:
            if model_context_window is not None and model_context_window <= 0:
                raise ValueError("model_context_window must be positive")
            if context_budget_cap is not None and context_budget_cap <= 0:
                raise ValueError("context_budget_cap must be positive")
            if model_context_window is None:
                if context_budget_cap is None:
                    raise ValueError(
                        "model_context_window is required when no context budget cap is configured"
                    )
                effective = int(context_budget_cap)
                capability_fallback = True
            elif context_budget_cap is None:
                effective = int(model_context_window)
            else:
                effective = min(int(model_context_window), int(context_budget_cap))

            request_max = int(request_max_output_tokens or 0)
            if request_max < 0:
                raise ValueError("request_max_output_tokens cannot be negative")
            if model_max_output_tokens is not None and model_max_output_tokens <= 0:
                raise ValueError("model_max_output_tokens must be positive")
            reserved_output = min(request_max, int(model_max_output_tokens or request_max))

        if effective <= 0:
            raise ValueError("effective context window must be positive")
        if safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens cannot be negative")
        if reserved_output + safety_margin_tokens + legacy_reserve >= effective:
            raise ValueError("reserved tokens must be smaller than context window")

        self._total = effective
        self._model_context_window = model_context_window
        self._context_budget_cap = context_budget_cap
        self._reserved_output_tokens = int(reserved_output)
        self._safety_margin_tokens = int(safety_margin_tokens)
        self._legacy_reserve_tokens = int(legacy_reserve)
        self._counter = token_counter or LegacyTokenCounter()
        self._capability_fallback = bool(capability_fallback)
        if bridge_is_model_aware and self._capability_fallback:
            warnings.warn(
                "Model context_window is unknown; Forge is using the configured "
                "context budget cap as a compatibility fallback, not as model capability.",
                RuntimeWarning,
                stacklevel=2,
            )

    @property
    def counter(self) -> TokenCounter:
        return self._counter

    @property
    def effective_context_window(self) -> int:
        return self._total

    @property
    def reserved_output_tokens(self) -> int:
        return self._reserved_output_tokens

    @property
    def safety_margin_tokens(self) -> int:
        return self._safety_margin_tokens

    @property
    def available_input_tokens(self) -> int:
        return (
            self._total
            - self._reserved_output_tokens
            - self._safety_margin_tokens
            - self._legacy_reserve_tokens
        )

    @property
    def capability_fallback(self) -> bool:
        return self._capability_fallback

    def count_text(self, text: str) -> int:
        return self._counter.count_text(text)

    def count_messages(self, messages: list[dict]) -> int:
        return self._counter.count_messages(messages)

    def count_tool_schemas(self, tools) -> int:
        return self._counter.count_tool_schemas(tools)

    def default_plan(self) -> BudgetPlan:
        reserve = (
            self._reserved_output_tokens
            + self._safety_margin_tokens
            + self._legacy_reserve_tokens
        )
        available = self._total - reserve
        return BudgetPlan(
            total=self._total,
            reserve=reserve,
            reserved_output_tokens=self._reserved_output_tokens,
            safety_margin_tokens=self._safety_margin_tokens,
            legacy_reserve_tokens=self._legacy_reserve_tokens,
            system_core=int(available * 0.10),
            repo_map=int(available * 0.15),
            history=int(available * 0.50),
            observation=int(available * 0.25),
        )

    def request_pressure(
        self,
        *,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        tools=(),
    ) -> ContextPressure:
        plan = self.default_plan()
        system_tokens = self.count_text(system_text)
        tool_schema_tokens = self.count_tool_schemas(tools)
        repo_map_tokens = self.count_text(repo_map_text) if repo_map_text else 0
        history_tokens = self.count_messages(history)
        # Repo Map is already embedded in the system prompt; keep it diagnostic-only.
        projected_input = system_tokens + tool_schema_tokens + history_tokens
        return ContextPressure(
            total=plan.total,
            reserve=plan.reserve,
            available_input=plan.available,
            system_tokens=system_tokens,
            tool_schema_tokens=tool_schema_tokens,
            repo_map_tokens=repo_map_tokens,
            history_tokens=history_tokens,
            projected_input=projected_input,
            reserved_output_tokens=self._reserved_output_tokens,
            safety_margin_tokens=self._safety_margin_tokens,
            counter_name=self._counter.name,
            capability_fallback=self._capability_fallback,
        )

    def history_limit_for_request(self, system_text: str, tools=()) -> int:
        plan = self.default_plan()
        fixed_tokens = self.count_text(system_text) + self.count_tool_schemas(tools)
        remaining = max(0, plan.available - fixed_tokens)
        return min(plan.history, remaining)

    def trim_to(self, text: str, token_limit: int) -> str:
        if token_limit <= 0:
            return ""
        if self.count_text(text) <= token_limit:
            return text
        if self.count_text(_TRUNCATION_SUFFIX) > token_limit:
            return self._trim_prefix(text, token_limit)

        low = 0
        high = len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = text[:midpoint] + _TRUNCATION_SUFFIX
            if self.count_text(candidate) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        result = text[:low] + _TRUNCATION_SUFFIX
        if self.count_text(result) > token_limit:
            return self._trim_prefix(text, token_limit)
        return result

    def trim_history(self, messages: list[dict], token_limit: int) -> list[dict]:
        if not messages or token_limit <= 0:
            return []
        if self.count_messages(messages) <= token_limit:
            return messages

        first = dict(messages[0])
        if self._counter.count_message(first) > token_limit:
            trimmed = self._trim_first_message(first, token_limit)
            return [trimmed] if trimmed is not None else []

        units = self._conversation_units(messages)
        selected_indices = self._select_units_dp(messages, units, token_limit, first)
        result = self._build_trimmed_history(messages, selected_indices, first)
        if self.count_messages(result) <= token_limit:
            return result
        return [first]

    def _trim_first_message(self, first: dict, token_limit: int) -> dict | None:
        empty = dict(first)
        empty["content"] = ""
        if self._counter.count_message(empty) > token_limit:
            return None

        content = str(first.get("content", ""))
        low = 0
        high = len(content)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = dict(first)
            candidate["content"] = content[:midpoint] + _TRUNCATION_SUFFIX
            if self._counter.count_message(candidate) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        trimmed = dict(first)
        if low > 0:
            trimmed["content"] = content[:low] + _TRUNCATION_SUFFIX
        else:
            trimmed["content"] = self._trim_prefix(content, max(1, token_limit - 5))
        while trimmed["content"] and self._counter.count_message(trimmed) > token_limit:
            trimmed["content"] = trimmed["content"][:-1]
        return trimmed if self._counter.count_message(trimmed) <= token_limit else None

    def _trim_prefix(self, text: str, token_limit: int) -> str:
        if token_limit <= 0:
            return ""
        low = 0
        high = len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.count_text(text[:midpoint]) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        candidate = text[:low]
        while candidate and self.count_text(candidate) > token_limit:
            candidate = candidate[:-1]
        return candidate

    def _conversation_units(self, messages: list[dict]) -> list[HistoryUnit]:
        return history_units(messages)

    def _select_units_dp(
        self,
        messages: list[dict],
        units: list[HistoryUnit],
        token_limit: int,
        first: dict,
    ) -> set[int]:
        first_cost = self._counter.count_message(first)
        states: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {
            (first_cost, 0): (0, ()),
        }
        for unit in units:
            next_states: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {}
            unit_cost = history_unit_tokens(messages, unit, counter=self._counter)
            for (used, gap_count), (utility, selected) in states.items():
                self._store_state(
                    next_states,
                    (used, gap_count + unit.message_count),
                    utility,
                    selected,
                )

                notice_cost = 0
                if gap_count:
                    notice_cost = self._counter.count_message(self._history_notice(gap_count))
                kept_cost = used + notice_cost + unit_cost
                if kept_cost <= token_limit:
                    unit_utility = unit.message_count * 10_000 + unit.recency
                    self._store_state(
                        next_states,
                        (kept_cost, 0),
                        utility + unit_utility,
                        selected + unit.indices,
                    )
            states = self._prune_states(next_states)

        best: tuple[int, int, tuple[int, ...]] | None = None
        for (used, gap_count), (utility, selected) in states.items():
            final_cost = used
            if gap_count:
                final_cost += self._counter.count_message(self._history_notice(gap_count))
            if final_cost > token_limit:
                continue
            candidate = (utility, -final_cost, selected)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        return set(best[2]) if best else set()

    @staticmethod
    def _store_state(states, key, utility, selected) -> None:
        current = states.get(key)
        if current is None or utility > current[0]:
            states[key] = (utility, selected)

    @staticmethod
    def _prune_states(states):
        pruned = {}
        by_gap: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {}
        for (used, gap), (utility, selected) in states.items():
            by_gap.setdefault(gap, []).append((used, utility, selected))
        for gap, candidates in by_gap.items():
            best_utility = -1
            for used, utility, selected in sorted(candidates):
                if utility > best_utility:
                    pruned[(used, gap)] = (utility, selected)
                    best_utility = utility
        return pruned

    def _history_notice(self, dropped: int) -> dict:
        noun = "message" if dropped == 1 else "messages"
        return {
            "role": "user",
            "content": f"[{dropped} {noun} omitted here to fit context window]",
        }

    def _build_trimmed_history(
        self,
        messages: list[dict],
        selected_indices: set[int],
        first: dict,
    ) -> list[dict]:
        result = [first]
        cursor = 1
        for index in sorted(selected_indices):
            dropped = index - cursor
            if dropped > 0:
                result.append(self._history_notice(dropped))
            result.append(messages[index])
            cursor = index + 1
        dropped_tail = len(messages) - cursor
        if dropped_tail > 0:
            result.append(self._history_notice(dropped_tail))
        return result

    def fit_all(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> tuple[str, str, list[dict], str]:
        plan = self.default_plan()
        return (
            self.trim_to(system_text, plan.system_core),
            self.trim_to(repo_map_text, plan.repo_map),
            self.trim_history(history, plan.history),
            self.trim_to(observation_text, plan.observation),
        )

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> dict[str, int | str | bool]:
        history_tokens = self.count_messages(history)
        system_tokens = self.count_text(system_text)
        repo_tokens = self.count_text(repo_map_text)
        observation_tokens = self.count_text(observation_text)
        return {
            "system": system_tokens,
            "repo_map": repo_tokens,
            "history": history_tokens,
            "observation": observation_tokens,
            "total": system_tokens + repo_tokens + history_tokens + observation_tokens,
            "budget": self._total,
            "available_input": self.available_input_tokens,
            "reserved_output": self._reserved_output_tokens,
            "safety_margin": self._safety_margin_tokens,
            "legacy_reserve": self._legacy_reserve_tokens,
            "counter": self._counter.name,
            "estimated": True,
            "capability_fallback": self._capability_fallback,
            "tiktoken_used": self._counter.name.startswith("tiktoken:"),
        }
