"""ipc/ — 单进程内异步消息总线（M2 Task 2.1）。

AgentBus：基于 asyncio.Queue 的纯 pub/sub 推送，替代 s20 里基于文件轮询的 MessageBus。
"""

from ipc.bus import AgentBus, Message

__all__ = ["AgentBus", "Message"]
