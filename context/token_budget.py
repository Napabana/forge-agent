"""Token budgeting for prompt sections and conversation history."""

from __future__ import annotations

from dataclasses import dataclass


_tiktoken_enc = None
_tiktoken_available = False
_MESSAGE_PROTOCOL_TOKENS = 4
_TRUNCATION_SUFFIX = "\n... [tokens truncated]"


def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    try:
        import tiktoken

        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """Estimate text tokens with tiktoken and a conservative fallback."""

    if not _tiktoken_available:
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
    """Estimate content plus role/metadata and chat protocol framing."""

    total = _MESSAGE_PROTOCOL_TOKENS
    total += estimate_tokens(str(message.get("role", "")))
    total += estimate_tokens(str(message.get("content", "")))
    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if value:
            total += estimate_tokens(str(value)) + 1
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate a full message list, including per-message framing."""

    return sum(estimate_message_tokens(message) for message in messages)


@dataclass
class BudgetPlan:
    total: int
    system_core: int
    repo_map: int
    history: int
    observation: int
    reserve: int

    @property
    def available(self) -> int:
        return self.total - self.reserve


@dataclass(frozen=True)
class _HistoryUnit:
    """An indivisible assistant/action + user/observation exchange."""

    indices: tuple[int, ...]

    @property
    def message_count(self) -> int:
        return len(self.indices)

    @property
    def recency(self) -> int:
        return sum(self.indices)


class TokenBudget:
    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def default_plan(self) -> BudgetPlan:
        reserve = int(self._total * 0.15)
        available = self._total - reserve
        return BudgetPlan(
            total=self._total,
            reserve=reserve,
            system_core=int(available * 0.10),
            repo_map=int(available * 0.15),
            history=int(available * 0.50),
            observation=int(available * 0.25),
        )

    def trim_to(self, text: str, token_limit: int) -> str:
        """Return the longest binary-searched prefix that fits with its notice."""

        if token_limit <= 0:
            return ""
        if estimate_tokens(text) <= token_limit:
            return text
        if estimate_tokens(_TRUNCATION_SUFFIX) > token_limit:
            return self._trim_prefix(text, token_limit)

        low = 0
        high = len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = text[:midpoint] + _TRUNCATION_SUFFIX
            if estimate_tokens(candidate) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        result = text[:low] + _TRUNCATION_SUFFIX
        # The final aggregate is checked because tokenizer boundaries can differ
        # from the sum of independently estimated body and suffix tokens.
        if estimate_tokens(result) > token_limit:
            return self._trim_prefix(text, token_limit)
        return result

    def trim_history(self, messages: list[dict], token_limit: int) -> list[dict]:
        """Select whole dialogue units globally with a dynamic program."""

        if not messages or token_limit <= 0:
            return []
        if estimate_messages_tokens(messages) <= token_limit:
            return messages

        first = dict(messages[0])
        if estimate_message_tokens(first) > token_limit:
            trimmed = self._trim_first_message(first, token_limit)
            return [trimmed] if trimmed is not None else []

        units = self._conversation_units(messages)
        selected_indices = self._select_units_dp(
            messages,
            units,
            token_limit,
            first,
        )
        result = self._build_trimmed_history(messages, selected_indices, first)
        if estimate_messages_tokens(result) <= token_limit:
            return result
        # Defensive fallback: the DP uses the same estimator, so this should only
        # be reachable if a caller mutates a message while selection is running.
        return [first]

    def _trim_first_message(self, first: dict, token_limit: int) -> dict | None:
        empty = dict(first)
        empty["content"] = ""
        if estimate_message_tokens(empty) > token_limit:
            return None

        content = str(first.get("content", ""))
        low = 0
        high = len(content)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = dict(first)
            candidate["content"] = content[:midpoint] + _TRUNCATION_SUFFIX
            if estimate_message_tokens(candidate) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        trimmed = dict(first)
        if low > 0:
            trimmed["content"] = content[:low] + _TRUNCATION_SUFFIX
        else:
            trimmed["content"] = self._trim_prefix(content, max(1, token_limit - 5))
        while (
            trimmed["content"]
            and estimate_message_tokens(trimmed) > token_limit
        ):
            trimmed["content"] = trimmed["content"][:-1]
        return trimmed if estimate_message_tokens(trimmed) <= token_limit else None

    def _trim_prefix(self, text: str, token_limit: int) -> str:
        """Binary-search the longest prefix accepted by the active estimator."""

        if token_limit <= 0:
            return ""
        low = 0
        high = len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            if estimate_tokens(text[:midpoint]) <= token_limit:
                low = midpoint
            else:
                high = midpoint - 1
        candidate = text[:low]
        while candidate and estimate_tokens(candidate) > token_limit:
            candidate = candidate[:-1]
        return candidate

    def _conversation_units(self, messages: list[dict]) -> list[_HistoryUnit]:
        """Group action/observation pairs so trimming never leaves half a turn."""

        units: list[_HistoryUnit] = []
        index = 1
        while index < len(messages):
            role = messages[index].get("role")
            if (
                role == "assistant"
                and index + 1 < len(messages)
                and messages[index + 1].get("role") in {"user", "tool"}
            ):
                units.append(_HistoryUnit((index, index + 1)))
                index += 2
            else:
                units.append(_HistoryUnit((index,)))
                index += 1
        return units

    def _select_units_dp(
        self,
        messages: list[dict],
        units: list[_HistoryUnit],
        token_limit: int,
        first: dict,
    ) -> set[int]:
        """Optimize kept message count, then recency, under the exact budget."""

        first_cost = estimate_message_tokens(first)
        # (used tokens, open-gap message count) -> (utility, selected indices)
        states: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {
            (first_cost, 0): (0, ()),
        }
        for unit in units:
            next_states: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {}
            unit_cost = sum(estimate_message_tokens(messages[i]) for i in unit.indices)
            for (used, gap_count), (utility, selected) in states.items():
                self._store_state(
                    next_states,
                    (used, gap_count + unit.message_count),
                    utility,
                    selected,
                )

                notice_cost = 0
                if gap_count:
                    notice_cost = estimate_message_tokens(self._history_notice(gap_count))
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
                final_cost += estimate_message_tokens(self._history_notice(gap_count))
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
        """Remove higher-cost states that cannot beat a cheaper equivalent gap."""

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
    ) -> dict[str, int]:
        history_tokens = estimate_messages_tokens(history)
        system_tokens = estimate_tokens(system_text)
        repo_tokens = estimate_tokens(repo_map_text)
        observation_tokens = estimate_tokens(observation_text)
        return {
            "system": system_tokens,
            "repo_map": repo_tokens,
            "history": history_tokens,
            "observation": observation_tokens,
            "total": system_tokens + repo_tokens + history_tokens + observation_tokens,
            "budget": self._total,
            "tiktoken_used": is_tiktoken_available(),
        }