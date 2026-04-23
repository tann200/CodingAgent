"""mcp_sse_client.py — MCP client using HTTP+SSE transport (TASK-9).

Implements the Model Context Protocol over an HTTP+Server-Sent Events
connection, matching the ``McpStdioClient`` interface so callers can switch
transports by swapping the client class.

Transport spec
--------------
- **POST** ``{base_url}/message``  — send a JSON-RPC request.
- **GET**  ``{base_url}/sse``      — receive streamed JSON-RPC responses.

Each SSE event carries one JSON-RPC envelope in its ``data`` field.
Requests are sent independently over HTTP POST; their responses arrive
asynchronously via the SSE stream, matched by ``id``.

Dependency
----------
Requires ``aiohttp`` (optional).  If absent, ``connect()`` raises
``ImportError`` with a helpful message.

Usage::

    from src.core.mcp.mcp_sse_client import McpSseClient

    client = McpSseClient(name="myserver", url="http://localhost:3000")
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/tmp/hello.txt"})
    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # aiohttp is an optional runtime dependency; import only for type checking
    pass  # type: ignore

from src.core.mcp.mcp_client import McpToolDefinition, McpToolResult

logger = logging.getLogger(__name__)

_INITIALIZE_TIMEOUT = 10.0
_CALL_TIMEOUT = 30.0
_JSONRPC_VERSION = "2.0"


class McpSseClient:
    """Async MCP client using HTTP+SSE transport.

    Parameters
    ----------
    name:
        Human-readable server name (used as tool origin tag).
    url:
        Base URL of the MCP server (e.g. ``"http://localhost:3000"``).
        ``/message`` and ``/sse`` paths are appended automatically.
    headers:
        Optional extra HTTP headers (e.g. ``{"Authorization": "Bearer …"}``).
    """

    def __init__(
        self,
        name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.url = url.rstrip("/")
        self._extra_headers: Dict[str, str] = headers or {}
        self.tools: List[McpToolDefinition] = []
        self._request_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._connected: bool = False
        self._session: Optional[Any] = None  # aiohttp.ClientSession
        self._sse_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        """Return an awaitable that opens the SSE stream and runs the MCP initialize handshake.

        This method intentionally performs the ``aiohttp`` import check synchronously so
        callers that attempt to call ``connect()`` in a synchronous context (for example
        via ``asyncio.get_event_loop().run_until_complete(c.connect())``) will observe
        an immediate ImportError when aiohttp is not available.  When aiohttp is present
        this returns a coroutine which must be awaited.
        """
        try:
            import aiohttp  # noqa: F401  # type: ignore[reportMissingImports]
        except ImportError as exc:
            # Raise synchronously so callers that evaluate c.connect() see the ImportError
            raise ImportError(
                "McpSseClient requires 'aiohttp'. Install it with: pip install aiohttp"
            ) from exc

        async def _connect_impl() -> None:
            if self._connected:
                return

            self._session = aiohttp.ClientSession(headers=self._extra_headers)
            self._sse_task = asyncio.create_task(
                self._sse_read_loop(), name=f"mcp-sse-{self.name}"
            )

            try:
                await self._initialize()
            except Exception as exc:
                logger.warning(
                    "McpSseClient[%s]: initialize failed: %s", self.name, exc
                )
                await self._cleanup()
                raise

            self._connected = True
            logger.info("McpSseClient[%s]: connected to %s", self.name, self.url)

        return _connect_impl()

    async def disconnect(self) -> None:
        """Close the SSE stream and HTTP session."""
        self._connected = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sse_task = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ------------------------------------------------------------------
    # SSE reader
    # ------------------------------------------------------------------

    async def _sse_read_loop(self) -> None:
        """Background task: read SSE events and resolve pending futures."""
        try:
            import aiohttp  # type: ignore[reportMissingImports]  # noqa: F401
        except ImportError:
            return
        assert self._session is not None
        sse_url = f"{self.url}/sse"
        try:
            async with self._session.get(sse_url) as resp:
                resp.raise_for_status()
                async for line_bytes in resp.content:
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        msg = json.loads(data_str)
                    except json.JSONDecodeError:
                        logger.debug(
                            "McpSseClient[%s]: malformed SSE data: %r",
                            self.name,
                            data_str[:120],
                        )
                        continue
                    self._dispatch_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("McpSseClient[%s]: SSE read loop error: %s", self.name, exc)

    def _dispatch_message(self, msg: Dict[str, Any]) -> None:
        """Route an inbound JSON-RPC message to the waiting future."""
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(
                        RuntimeError(
                            f"MCP error: {msg['error'].get('message', msg['error'])}"
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))

    # ------------------------------------------------------------------
    # JSON-RPC send
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = _CALL_TIMEOUT,
    ) -> Any:
        """Send a JSON-RPC request via POST and await the SSE response."""
        if self._session is None:
            raise RuntimeError("McpSseClient: not connected")

        req_id = self._next_id()
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        try:
            async with self._session.post(
                f"{self.url}/message",
                json=payload,
            ) as resp:
                resp.raise_for_status()
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"McpSseClient: POST failed: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(
                f"McpSseClient[{self.name}]: no response for '{method}' in {timeout}s"
            )

    # ------------------------------------------------------------------
    # MCP handshake
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        result = await self._send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "CodingAgent", "version": "1.0"},
            },
            timeout=_INITIALIZE_TIMEOUT,
        )
        logger.debug("McpSseClient[%s]: initialize result: %s", self.name, result)
        # Send initialized notification (no response expected)
        try:
            assert self._session is not None
            await self._session.post(
                f"{self.url}/message",
                json={
                    "jsonrpc": _JSONRPC_VERSION,
                    "method": "notifications/initialized",
                    "params": {},
                },
            )
        except Exception:
            pass  # notification failure is non-fatal

    # ------------------------------------------------------------------
    # Tool API
    # ------------------------------------------------------------------

    async def list_tools(self) -> List[McpToolDefinition]:
        """Query the server for available tools and cache them."""
        result = await self._send("tools/list")
        raw_tools = result.get("tools", []) if isinstance(result, dict) else []
        self.tools = [
            McpToolDefinition(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            for t in raw_tools
            if isinstance(t, dict) and t.get("name")
        ]
        return self.tools

    async def call_tool(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> McpToolResult:
        """Call an MCP tool by name with the given arguments."""
        try:
            result = await self._send(
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
            )
            content = result.get("content") if isinstance(result, dict) else result
            return McpToolResult(ok=True, content=content)
        except Exception as exc:
            return McpToolResult(ok=False, error=str(exc))

    async def register_tools(self, registry: Any) -> int:
        """Discover tools from the server and register them in *registry*.

        Returns the number of tools registered.
        """
        tools = await self.list_tools()
        count = 0
        for tool_defn in tools:
            _name = f"{self.name}/{tool_defn.name}"
            _client_ref = self
            _tool_name = tool_defn.name

            def _make_fn(c: "McpSseClient", tn: str):
                async def _fn(**kwargs: Any) -> Dict[str, Any]:
                    result = await c.call_tool(tn, kwargs)
                    if result.ok:
                        return {"ok": True, "output": result.content}
                    return {"ok": False, "error": result.error}

                _fn.__name__ = tn
                _fn.__doc__ = tool_defn.description
                return _fn

            try:
                registry.register(
                    _name,
                    _make_fn(_client_ref, _tool_name),
                    description=tool_defn.description,
                    origin="plugin",
                )
                count += 1
            except Exception as exc:
                logger.warning(
                    "McpSseClient[%s]: failed to register tool %s: %s",
                    self.name,
                    _name,
                    exc,
                )
        return count
