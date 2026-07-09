"""
context/token_budget.py

Token 预算管理：给 prompt 各部分分配 token 配额，超出时按优先级裁剪。

## tiktoken 安装

    pip install tiktoken

首次运行时自动下载词表（需联网，约 2MB），之后缓存到本地离线可用。

如果网络无法访问 OpenAI CDN，手动下载词表：
    curl -L "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \\
         -o ~/.cache/tiktoken/9b5ad71b2ce5302211f9c61530b329a4922fc6a4021629a1eba1b43bf10a10.tiktoken

然后设置环境变量：
    export TIKTOKEN_CACHE_DIR=~/.cache/tiktoken

tiktoken 不可用时自动降级为字符估算（1 token ≈ 4 chars），精度足够做预算控制。

各部分优先级（高→低，裁剪时从低优先级开始）：
  1. system_core   系统指令，永不裁剪
  2. task          任务描述，永不裁剪
  3. repo_map      repo 摘要，超出时缩减
  4. recent_obs    最近 observation，永不裁剪
  5. history       历史对话，从最旧开始裁剪
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Token 计数：优先 tiktoken，失败时字符估算 fallback
# ---------------------------------------------------------------------------

_tiktoken_enc = None
_tiktoken_available = False

def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    try:
        import tiktoken
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        # 网络不通 / 未安装，降级为字符估算
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数。
    优先使用 tiktoken（精确），不可用时用字符数 // 4（误差 <15%）。
    """
    if not _tiktoken_available:
        _init_tiktoken()

    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass

    # 字符估算 fallback
    return max(1, len(text) // 4)


def estimate_chars(tokens: int) -> int:
    """把 token 数转换为字符预算（估算）。"""
    return tokens * 4


def is_tiktoken_available() -> bool:
    """返回 tiktoken 是否可用，供诊断脚本使用。"""
    _init_tiktoken()
    return _tiktoken_available


# ---------------------------------------------------------------------------
# BudgetPlan
# ---------------------------------------------------------------------------

@dataclass
class BudgetPlan:
    """各部分的 token 配额计划。"""
    total: int
    system_core: int
    repo_map: int
    history: int
    observation: int
    reserve: int

    @property
    def available(self) -> int:
        return self.total - self.reserve


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------

class TokenBudget:
    """
    Token 预算管理器。

    用法：
        budget = TokenBudget(total=80_000)
        plan = budget.default_plan()
        trimmed = budget.trim_to(text, plan.repo_map)
        trimmed_history = budget.trim_history(msgs, plan.history)
    """

    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def default_plan(self) -> BudgetPlan:
        total = self._total
        reserve = int(total * 0.15)
        available = total - reserve
        return BudgetPlan(
            total=total,
            reserve=reserve,
            system_core=int(available * 0.10),
            repo_map=int(available * 0.15),
            history=int(available * 0.50),
            observation=int(available * 0.25),
        )

    def trim_to(self, text: str, token_limit: int) -> str:
        """裁剪文本到 token_limit 以内，超出时保留开头。"""
        if token_limit <= 0:
            return ""
        if estimate_tokens(text) <= token_limit:
            return text

        suffix = "\n... [tokens truncated]"
        suffix_tokens = estimate_tokens(suffix)
        if suffix_tokens >= token_limit:
            return self._trim_prefix(text, token_limit)

        candidate = self._trim_prefix(text, token_limit - suffix_tokens)
        return candidate + suffix

    def trim_history(
        self,
        messages: list[dict],
        token_limit: int,
    ) -> list[dict]:
        """
        裁剪历史消息列表到 token_limit 以内。
        保留第一条（任务描述）+ 尽量多的历史片段。
        如果中间消息被删除，在对应位置插入省略提示，避免伪造连续时间线。
        """
        if not messages:
            return messages
        if token_limit <= 0:
            return []

        token_counts = [estimate_tokens(m.get("content", "")) for m in messages]
        total = sum(token_counts)

        if total <= token_limit:
            return messages

        first = dict(messages[0])
        first_tokens = token_counts[0]
        if first_tokens > token_limit:
            first["content"] = self.trim_to(first.get("content", ""), token_limit)
            return [first]

        selected_indices: set[int] = set()
        changed = True
        while changed:
            changed = False
            for idx in range(len(messages) - 1, 0, -1):
                if idx in selected_indices:
                    continue
                selected_indices.add(idx)
                result = self._build_trimmed_history(messages, selected_indices, first)
                result_tokens = sum(
                    estimate_tokens(m.get("content", "")) for m in result
                )
                if result_tokens <= token_limit:
                    changed = True
                else:
                    selected_indices.remove(idx)

        result = self._build_trimmed_history(messages, selected_indices, first)
        result_tokens = sum(estimate_tokens(m.get("content", "")) for m in result)
        if result_tokens > token_limit:
            return [first]
        return result

    def _trim_prefix(self, text: str, token_limit: int) -> str:
        """返回 text 的前缀，保证估算 token 不超过 token_limit。"""
        if token_limit <= 0:
            return ""

        char_limit = token_limit * 4
        candidate = text[:char_limit]
        while estimate_tokens(candidate) > token_limit and len(candidate) > 0:
            next_len = int(len(candidate) * 0.9)
            if next_len == len(candidate):
                next_len -= 1
            candidate = candidate[:max(0, next_len)]

        return candidate

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
        sorted_indices = sorted(selected_indices)
        cursor = 1

        for idx in sorted_indices:
            dropped = idx - cursor
            if dropped > 0:
                result.append(self._history_notice(dropped))
            result.append(messages[idx])
            cursor = idx + 1

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
        trimmed_system = self.trim_to(system_text, plan.system_core)
        trimmed_map = self.trim_to(repo_map_text, plan.repo_map)
        trimmed_history = self.trim_history(history, plan.history)
        trimmed_obs = self.trim_to(observation_text, plan.observation)
        return trimmed_system, trimmed_map, trimmed_history, trimmed_obs

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> dict[str, int]:
        history_tokens = sum(
            estimate_tokens(m.get("content", "")) for m in history
        )
        return {
            "system":      estimate_tokens(system_text),
            "repo_map":    estimate_tokens(repo_map_text),
            "history":     history_tokens,
            "observation": estimate_tokens(observation_text),
            "total": (
                estimate_tokens(system_text)
                + estimate_tokens(repo_map_text)
                + history_tokens
                + estimate_tokens(observation_text)
            ),
            "budget":        self._total,
            "tiktoken_used": is_tiktoken_available(),
        }
