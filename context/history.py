"""Canonical conversation history storage.

ConversationHistory now keeps the logical conversation intact. Prompt-size control belongs
to TokenBudget and compaction policies, so message-count limits no longer delete evidence
before those policies can inspect it.
"""

from __future__ import annotations

from llm.base import LLMMessage


class ConversationHistory:
    """维护完整逻辑历史；模型可见窗口由 Context/TokenBudget 单独构造。"""

    def __init__(self, max_messages: int | None = 40) -> None:
        """
        Args:
            max_messages: 兼容旧配置的消息窗口提示。C1 起不再据此永久删除历史；
                          真正发给模型的内容由 TokenBudget/Compaction 控制。
        """
        self._messages: list[LLMMessage] = []
        self._max = max_messages

    def add(self, message: LLMMessage) -> None:
        """追加一条逻辑历史，不在事实存储层按消息数销毁旧内容。"""
        self._messages.append(message)

    def add_many(self, messages: list[LLMMessage]) -> None:
        """批量追加逻辑历史，不在事实存储层做 destructive trim。"""
        self._messages.extend(messages)

    def to_list(self) -> list[LLMMessage]:
        """返回完整逻辑历史列表（浅拷贝）。"""
        return list(self._messages)

    def to_dicts(self) -> list[dict]:
        """转为 dict 列表，供 TokenBudget/Compaction 构造模型可见上下文。"""
        return [
            {
                "role": message.role,
                "content": message.content,
                **({"event_ref": message.event_ref} if message.event_ref else {}),
            }
            for message in self._messages
        ]

    @classmethod
    def from_dicts(cls, dicts: list[dict], max_messages: int | None = 40) -> "ConversationHistory":
        """从持久化数据恢复完整逻辑历史。"""
        history = cls(max_messages=max_messages)
        history._messages = [
            LLMMessage(
                role=item["role"],
                content=item["content"],
                event_ref=item.get("event_ref"),
            )
            for item in dicts
        ]
        return history

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def last_message(self) -> LLMMessage | None:
        return self._messages[-1] if self._messages else None

    def clear_except_first(self) -> None:
        """保留首条任务描述，清除其余（紧急重置用）。"""
        if self._messages:
            self._messages = [self._messages[0]]

    def clear(self) -> None:
        """清除全部消息，用于显式重置 Chat 会话上下文。"""
        self._messages.clear()

    def replace(self, messages: list[LLMMessage]) -> None:
        """原子替换逻辑历史；Compaction 后续会迁移为独立 Context State。"""
        self._messages = list(messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:
        return f"ConversationHistory(messages={len(self._messages)}, max_hint={self._max})"
