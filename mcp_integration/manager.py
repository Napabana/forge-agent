from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass
from typing import Any

from mcp import Client, MCPError, StdioServerParameters

from config.schema import MCPConfig, MCPServerConfig


class MCPIntegrationError(RuntimeError):
    """MCP integration base error."""


class MCPManagerLifecycleError(MCPIntegrationError):
    """Forge-side MCP lifecycle invariant failed."""


class MCPCallTimeout(MCPIntegrationError):
    """One remote tool call exceeded its configured timeout."""


class MCPRemoteFailure(MCPIntegrationError):
    """Remote transport/server/protocol failure that does not corrupt Forge."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MCPToolDescriptor:
    server_id: str
    transport: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    timeout_seconds: float
    trust_read_only_annotations: bool


@dataclass(frozen=True)
class MCPServerSnapshot:
    server_id: str
    transport: str
    server_name: str | None
    server_version: str | None
    protocol_version: str | None
    tools_supported: bool
    resources_supported: bool
    prompts_supported: bool
    tool_count: int


@dataclass
class _Connection:
    config: MCPServerConfig
    client: Client
    snapshot: MCPServerSnapshot


class MCPClientManager:
    """Own official MCP clients on one dedicated asyncio loop thread.

    Client async contexts are entered and exited by the same supervisor task.
    Sync Forge tools submit only call coroutines to that loop, so a connection is
    not recreated for every ToolCall and no nested event loop is required.
    """

    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._start_error: BaseException | None = None
        self._connections: dict[str, _Connection] = {}
        self._tools: tuple[MCPToolDescriptor, ...] = ()

    @property
    def tools(self) -> tuple[MCPToolDescriptor, ...]:
        return self._tools

    @property
    def snapshots(self) -> tuple[MCPServerSnapshot, ...]:
        return tuple(connection.snapshot for connection in self._connections.values())

    @property
    def is_started(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._start_error is None
        )

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread is not None:
            if self._start_error is not None:
                raise self._classify_start_error(self._start_error)
            return
        # A manager normally starts once, but resetting these guards makes an
        # explicit close -> start cycle deterministic rather than reading stale state.
        self._ready.clear()
        self._closed.clear()
        self._start_error = None
        self._connections.clear()
        self._tools = ()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="forge-mcp-client-loop",
            daemon=True,
        )
        self._thread.start()
        startup_timeout = max(
            (s.timeout_seconds for s in self.config.servers if s.enabled),
            default=30.0,
        )
        if not self._ready.wait(timeout=startup_timeout + 5.0):
            self.close()
            raise MCPRemoteFailure("MCP server startup/discovery timed out")
        if self._start_error is not None:
            error = self._start_error
            self.close()
            raise self._classify_start_error(error) from error

    @staticmethod
    def _classify_start_error(error: BaseException) -> MCPIntegrationError:
        if isinstance(error, MCPIntegrationError):
            return error
        if isinstance(error, (OSError, TimeoutError, MCPError)):
            return MCPRemoteFailure(
                f"MCP server startup/discovery failed: "
                f"{type(error).__name__}: {error}"
            )
        return MCPManagerLifecycleError(
            f"MCP client manager startup invariant failed: "
            f"{type(error).__name__}: {error}"
        )

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._supervisor())
        except BaseException as exc:  # pragma: no cover - defensive thread boundary
            if not self._ready.is_set():
                self._start_error = exc
                self._ready.set()
        finally:
            self._closed.set()

    async def _supervisor(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        entered: list[Client] = []
        descriptors: list[MCPToolDescriptor] = []
        try:
            for server in self.config.servers:
                if not server.enabled:
                    continue
                client = self._make_client(server)
                async with asyncio.timeout(server.timeout_seconds):
                    await client.__aenter__()
                entered.append(client)
                capabilities = client.server_capabilities
                if getattr(capabilities, "tools", None) is not None:
                    async with asyncio.timeout(server.timeout_seconds):
                        tool_items = await self._list_all_tools(client)
                else:
                    tool_items = ()
                snapshot = self._snapshot(server, client, len(tool_items))
                self._connections[server.id] = _Connection(server, client, snapshot)
                for tool in tool_items:
                    annotations = (
                        tool.annotations.model_dump(exclude_none=True)
                        if getattr(tool, "annotations", None) is not None
                        else {}
                    )
                    descriptors.append(
                        MCPToolDescriptor(
                            server_id=server.id,
                            transport=server.transport,
                            remote_name=str(tool.name),
                            description=str(tool.description or ""),
                            input_schema=dict(tool.input_schema),
                            annotations=dict(annotations),
                            timeout_seconds=server.timeout_seconds,
                            trust_read_only_annotations=server.trust_read_only_annotations,
                        )
                    )
            self._tools = tuple(descriptors)
        except BaseException as exc:
            self._start_error = exc
        finally:
            self._ready.set()

        if self._start_error is None:
            assert self._shutdown is not None
            await self._shutdown.wait()

        for client in reversed(entered):
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                # Cleanup stays best-effort here; the owning thread still terminates.
                pass
        self._connections.clear()

    @staticmethod
    async def _list_all_tools(client: Client) -> tuple[Any, ...]:
        items: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            listing = await client.list_tools(cursor=cursor, cache_mode="bypass")
            items.extend(listing.tools)
            next_cursor = getattr(listing, "next_cursor", None)
            if not next_cursor:
                return tuple(items)
            if next_cursor in seen_cursors:
                raise MCPRemoteFailure("MCP tools/list returned a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = str(next_cursor)

    @staticmethod
    def _make_client(server: MCPServerConfig) -> Client:
        if server.transport == "stdio":
            target = StdioServerParameters(
                command=server.command or "",
                args=list(server.args),
                env=dict(server.env) or None,
            )
        elif server.transport == "streamable_http":
            target = server.url or ""
        else:  # config validation should make this unreachable
            raise MCPManagerLifecycleError(
                f"unsupported MCP transport {server.transport!r}"
            )
        return Client(target, read_timeout_seconds=server.timeout_seconds)

    @staticmethod
    def _snapshot(
        server: MCPServerConfig,
        client: Client,
        tool_count: int,
    ) -> MCPServerSnapshot:
        info = client.server_info
        capabilities = client.server_capabilities
        return MCPServerSnapshot(
            server_id=server.id,
            transport=server.transport,
            server_name=getattr(info, "name", None),
            server_version=getattr(info, "version", None),
            protocol_version=(
                str(client.protocol_version)
                if client.protocol_version is not None
                else None
            ),
            tools_supported=getattr(capabilities, "tools", None) is not None,
            resources_supported=getattr(capabilities, "resources", None) is not None,
            prompts_supported=getattr(capabilities, "prompts", None) is not None,
            tool_count=tool_count,
        )

    def call_tool(
        self,
        server_id: str,
        remote_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        if not self.is_started or self._loop is None:
            raise MCPManagerLifecycleError("MCP client manager is not running")
        connection = self._connections.get(server_id)
        if connection is None:
            raise MCPManagerLifecycleError(f"unknown MCP server id {server_id!r}")
        future = asyncio.run_coroutine_threadsafe(
            self._call(connection.client, remote_name, arguments, timeout_seconds),
            self._loop,
        )
        try:
            return future.result(timeout=timeout_seconds + 1.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise MCPCallTimeout(
                f"MCP tool {server_id}/{remote_name} timed out after "
                f"{timeout_seconds:g}s"
            ) from exc
        except MCPIntegrationError:
            raise
        except Exception as exc:
            raise MCPRemoteFailure(
                f"MCP tool {server_id}/{remote_name} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    async def _call(
        client: Client,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await client.call_tool(remote_name, arguments)
        except TimeoutError as exc:
            raise MCPCallTimeout(
                f"MCP call timed out after {timeout_seconds:g}s"
            ) from exc
        except MCPError as exc:
            error = getattr(exc, "error", None)
            code = getattr(error, "code", None)
            raise MCPRemoteFailure(
                str(exc),
                code=code if isinstance(code, int) else None,
            ) from exc
        except MCPIntegrationError:
            raise
        except Exception as exc:
            raise MCPRemoteFailure(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        loop = self._loop
        shutdown = self._shutdown
        if thread.is_alive() and loop is not None and shutdown is not None:
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:
                pass
        thread.join(timeout=10.0)
        if thread.is_alive():
            raise MCPManagerLifecycleError(
                "MCP client manager did not close deterministically"
            )
        self._thread = None
        self._loop = None
        self._shutdown = None
