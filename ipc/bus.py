"""
ipc/bus.py

单进程内异步消息总线（M2 Task 2.1）。

把 s20_comprehensive/code.py 的 MessageBus（文件邮箱轮询，code.py:480-502）
迁移成基于 asyncio.Queue 的纯 pub/sub 推送。

与 s20 的差异（写进设计）：
- 存储：内存 asyncio.Queue，不再落盘 JSONL 邮箱。
- 消费：订阅者用 async 迭代器 push 消费，不再轮询/读后删除。
- 持久化：无。重启丢失。M4 若需跨进程/持久化再上 Redis Pub/Sub
  （见 改进目标.md M4）。
- topic 路由：支持 "agent.*" 前缀通配订阅。

消息字段对齐 s20（from/to/content/type/metadata/ts），重命名为
sender/topic/content/msg_type/metadata/ts。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 消息载体
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """总线上一条消息。字段对齐 s20 MessageBus.send。"""
    sender: str                       # 发送者 agent 名
    topic: str                        # 目标 topic，如 "agent_b" / "tasks.completed"
    content: Any                      # 消息体（s20 里是 str，这里放宽为 Any）
    msg_type: str = "message"         # message | shutdown_request | plan_approval_request ...
    metadata: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: f"msg_{uuid4().hex[:12]}")


# ---------------------------------------------------------------------------
# AgentBus
# ---------------------------------------------------------------------------

class AgentBus:
    """
    纯 pub/sub 异步消息总线。

    用法：
        bus = AgentBus()
        async for msg in bus.subscribe("agent_b"):
            handle(msg)
        await bus.publish("agent_b", sender="lead", content="hi")

    语义：
    - publish 向「所有匹配 topic 的订阅者队列」各投一份（广播）。
    - 无订阅者时消息丢弃（pub/sub，记 debug 日志）。
    - subscribe 返回一个独立队列的 async 迭代器；unsubscribe 停止投递。
    - topic 支持 "xxx.*" 前缀通配：订阅 "agent.*" 会收到 "agent.lead" / "agent.worker"。
    """

    def __init__(self) -> None:
        # topic -> 订阅者队列列表
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def subscribe(self, topic: str, maxsize: int = 0) -> "asyncio.Queue":
        """
        订阅一个 topic，返回一个 asyncio.Queue。
        调用方用 `await q.get()` 消费，或用 messages() async 迭代器。
        重复 subscribe 同一 topic 会得到多个独立队列（每个都收到全量消息）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers[topic].append(q)
        return q

    async def messages(self, topic: str, maxsize: int = 0) -> AsyncIterator[Message]:
        """订阅并异步迭代消息。迭代器退出（break/GeneratorExit）时自动清理。"""
        q = self.subscribe(topic, maxsize=maxsize)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            self.unsubscribe(topic, q)

    def unsubscribe(self, topic: str, queue: "asyncio.Queue") -> bool:
        """移除一个订阅队列。返回是否确实移除了。"""
        subs = self._subscribers.get(topic)
        if not subs:
            return False
        try:
            subs.remove(queue)
        except ValueError:
            return False
        if not subs:
            del self._subscribers[topic]
        return True

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    async def publish(
        self,
        topic: str,
        sender: str,
        content: Any,
        msg_type: str = "message",
        metadata: dict | None = None,
    ) -> int:
        """
        向 topic 投递消息。返回实际投递到的订阅者数量。
        通配订阅："agent.*" 的订阅者会收到 "agent.xxx" 的消息。
        """
        msg = Message(
            sender=sender, topic=topic, content=content,
            msg_type=msg_type, metadata=metadata or {},
        )
        delivered = 0
        for sub_topic in list(self._subscribers.keys()):
            if _topic_matches(sub_topic, topic):
                for q in self._subscribers[sub_topic]:
                    try:
                        q.put_nowait(msg)
                        delivered += 1
                    except asyncio.QueueFull:
                        logger.warning(
                            "[bus] subscriber queue full (topic=%s), dropping msg %s",
                            sub_topic, msg.id,
                        )
        if delivered == 0:
            logger.debug("[bus] no subscriber for topic=%s, msg %s dropped", topic, msg.id)
        return delivered

    # ------------------------------------------------------------------
    # 内省
    # ------------------------------------------------------------------

    def subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, []))

    def topics(self) -> list[str]:
        return list(self._subscribers.keys())


def _topic_matches(subscription: str, topic: str) -> bool:
    """
    订阅 pattern 是否匹配某个 topic。
    - 精确匹配：subscription == topic
    - 前缀通配：subscription 以 ".*" 结尾时，匹配前缀 + 任意单层后缀
      例："agent.*" 匹配 "agent.lead" / "agent.worker"，但不匹配 "agent.lead.sub"
    """
    if subscription == topic:
        return True
    if subscription.endswith(".*"):
        prefix = subscription[:-2]
        # topic 必须是 prefix + "." + 单层（不含额外 "."）
        if not topic.startswith(prefix + "."):
            return False
        rest = topic[len(prefix) + 1:]
        return "." not in rest
    return False
