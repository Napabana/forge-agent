"""
tests/test_agent_bus.py

M2 Task 2.1：AgentBus 纯 pub/sub 异步消息总线测试。
覆盖：单订阅、多订阅广播、通配 topic 路由、unsubscribe、无订阅者丢弃。
"""

from __future__ import annotations

import asyncio

import pytest

from ipc.bus import AgentBus, Message


# ===========================================================================
# 基础 pub/sub
# ===========================================================================

class TestPubSub:
    async def test_single_subscriber_receives(self):
        bus = AgentBus()
        q = bus.subscribe("agent_b")
        await bus.publish("agent_b", sender="lead", content="hi")
        msg: Message = await asyncio.wait_for(q.get(), timeout=1)
        assert msg.sender == "lead"
        assert msg.topic == "agent_b"
        assert msg.content == "hi"
        assert msg.msg_type == "message"

    async def test_publish_returns_delivered_count(self):
        bus = AgentBus()
        bus.subscribe("t1")
        bus.subscribe("t1")
        n = await bus.publish("t1", sender="a", content="x")
        assert n == 2

    async def test_message_has_id_and_ts(self):
        bus = AgentBus()
        q = bus.subscribe("t")
        await bus.publish("t", sender="a", content="x")
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg.id.startswith("msg_")
        assert isinstance(msg.ts, float)


# ===========================================================================
# 多订阅者广播
# ===========================================================================

class TestBroadcast:
    async def test_multiple_subscribers_each_get_copy(self):
        bus = AgentBus()
        q1, q2 = bus.subscribe("events"), bus.subscribe("events")
        await bus.publish("events", sender="a", content={"k": 1})
        m1 = await asyncio.wait_for(q1.get(), timeout=1)
        m2 = await asyncio.wait_for(q2.get(), timeout=1)
        assert m1.content == {"k": 1}
        assert m2.content == {"k": 1}
        assert m1.id == m2.id   # 同一条消息广播

    async def test_independent_queues_isolate_consumption(self):
        bus = AgentBus()
        q1, q2 = bus.subscribe("t"), bus.subscribe("t")
        await bus.publish("t", sender="a", content=1)
        # q1 消费掉，q2 仍持有
        await asyncio.wait_for(q1.get(), timeout=1)
        assert q1.empty()
        assert not q2.empty()


# ===========================================================================
# 通配 topic 路由
# ===========================================================================

class TestWildcard:
    async def test_wildcard_matches_single_layer(self):
        bus = AgentBus()
        q = bus.subscribe("agent.*")
        n_lead = await bus.publish("agent.lead", sender="x", content="1")
        n_worker = await bus.publish("agent.worker", sender="x", content="2")
        assert n_lead == 1
        assert n_worker == 1
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        assert {m.topic for m in msgs} == {"agent.lead", "agent.worker"}

    async def test_wildcard_does_not_match_multilayer(self):
        bus = AgentBus()
        bus.subscribe("agent.*")
        # "agent.lead.sub" 含额外层，不匹配 "agent.*"
        n = await bus.publish("agent.lead.sub", sender="x", content="deep")
        assert n == 0

    async def test_exact_and_wildcard_both_match(self):
        bus = AgentBus()
        q_exact = bus.subscribe("agent.lead")
        q_wild = bus.subscribe("agent.*")
        n = await bus.publish("agent.lead", sender="x", content="hi")
        assert n == 2   # 精确 + 通配都收到
        await asyncio.wait_for(q_exact.get(), timeout=1)
        await asyncio.wait_for(q_wild.get(), timeout=1)


# ===========================================================================
# unsubscribe / 无订阅者
# ===========================================================================

class TestLifecycle:
    async def test_unsubscribe_stops_delivery(self):
        bus = AgentBus()
        q = bus.subscribe("t")
        assert bus.subscriber_count("t") == 1
        assert bus.unsubscribe("t", q) is True
        assert bus.subscriber_count("t") == 0
        n = await bus.publish("t", sender="a", content="x")
        assert n == 0
        assert q.empty()

    async def test_unsubscribe_wrong_queue_returns_false(self):
        bus = AgentBus()
        q1 = bus.subscribe("t")
        q2 = asyncio.Queue()   # 不属于该 topic
        assert bus.unsubscribe("t", q2) is False
        assert bus.subscriber_count("t") == 1

    async def test_no_subscriber_drops_message(self):
        bus = AgentBus()
        n = await bus.publish("nobody", sender="a", content="x")
        assert n == 0
        assert "nobody" not in bus.topics()


# ===========================================================================
# async 迭代器 messages()
# ===========================================================================

class TestAsyncIterator:
    async def test_messages_iterator_yields_and_cleans_up(self):
        bus = AgentBus()
        gen = bus.messages("t")

        async def producer():
            await asyncio.sleep(0)   # 让消费者先进入 await q.get()
            await bus.publish("t", sender="a", content="first")

        prod = asyncio.create_task(producer())
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1)
        await prod
        assert msg.content == "first"
        await gen.aclose()   # 模拟消费者退出 → finally 里 unsubscribe
        assert bus.subscriber_count("t") == 0
