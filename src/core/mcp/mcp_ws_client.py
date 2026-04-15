"""mcp_ws_client.py — MCP client using WebSocket transport (TASK-10).

Implements the Model Context Protocol over a persistent WebSocket connection,
matching the ``McpStdioClient`` interface so callers can switch transports by
swapping the client class.

Transport spec
--------------
Each JSON-RPC message is sent and received as a single WebSocket text frame.
Requests and responses are matched by ``id``; notifications (no ``id``) are
forwarded to optional notification handlers.

Dependency
----------
Requires ``aiohttp`` (optional).  If absent, ``connect()`` raises
``ImportError`` with a helpful message.

Usage::

    from src.core.mcp.mcp_ws_client import McpWsClient

    client = McpWsClient(name="myserver", url="ws://localhost:3000/ws")
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/tmp/hello.txt"})
    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp  # type: ignore

from src.core.mcp.mcp_client import McpToolDefinition, McpToolResult

logger = logging.getLogger(__name__)

_INITIALIZE_TIMEOUT = 10.0
_CALL_TIMEOUT = 30.0
_JSONRPC_VERSION = "2.0"


class McpWsClient:
    """Async MCP client using WebSocket transport.

    Parameters
    ----------
    name:
        Human-readable server name (used as tool origin tag).
    url:
        WebSocket URL of the MCP server (e.g. ``"ws://localhost:3000/ws"``).
    headers:
        Optional extra HTTP headers for the WebSocket upgrade request.
    """

    def __init__(
        self,
        name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.url = url
        self._extra_headers: Dict[str, str] = headers or {}
        self.tools: List[McpToolDefinition] = []
        self._request_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._connected: bool = False
        # Use TYPE_CHECKING-only aiohttp names in annotations so static type
        # checkers understand the expected types without importing aiohttp at
        # runtime (aiohttp is optional). Use string forward-references to avoid
        # runtime NameError when aiohttp is not installed.
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._session: Optional["aiohttp.ClientSession"] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._notification_handlers: List[
            Callable[[str, Dict[str, Any]], Optional[Awaitable[None]]]
        ] = []

    def add_notification_handler(
        self,
        handler: Callable[[str, Dict[str, Any]], Optional[Awaitable[None]]],
    ) -> None:
        """Register a callback for server-sent notifications."""
        if not callable(handler):
            raise TypeError("notification handler must be callable")
        self._notification_handlers.append(handler)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket connection and run the MCP ``initialize`` handshake."""
        if self._connected:
            return

        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except ImportError as exc:
            raise ImportError(
                "McpWsClient requires 'aiohttp'. Install it with: pip install aiohttp"
            ) from exc

        # Annotate as Any so static analyzers don't attempt to resolve aiohttp
        # symbols when aiohttp is not installed in the analysis environment.
        session: Any = aiohttp.ClientSession(headers=self._extra_headers)
        self._session = session
        try:
            # Use the local `session` variable to help static analyzers infer the
            # non-Optional ClientSession type before calling ws_connect.
            self._ws = await session.ws_connect(self.url)
        except Exception as exc:
            await self._cleanup()
            raise RuntimeError(
                f"McpWsClient[{self.name}]: WebSocket connect failed: {exc}"
            ) from exc

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-ws-{self.name}"
        )

        try:
            await self._initialize()
        except Exception as exc:
            logger.warning("McpWsClient[%s]: initialize failed: %s", self.name, exc)
            await self._cleanup()
            raise

        self._connected = True
        logger.info("McpWsClient[%s]: connected to %s", self.name, self.url)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._ws = None

    # ------------------------------------------------------------------
    # WebSocket reader
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Background task: read WebSocket frames and dispatch messages."""
        if self._ws is None:
            return
        try:
            import aiohttp  # type: ignore[reportMissingImports]

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.debug(
                            "McpWsClient[%s]: malformed WS frame: %r",
                            self.name,
                            msg.data[:120],
                        )
                        continue
                    await self._dispatch_message(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.info(
                        "McpWsClient[%s]: WebSocket closed (type=%s)",
                        self.name,
                        msg.type,
                    )
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("McpWsClient[%s]: read loop error: %s", self.name, exc)

        # Fail any still-pending futures on disconnect
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    RuntimeError(f"McpWsClient[{self.name}]: connection closed")
                )
        self._pending.clear()

    async def _dispatch_message(self, msg: Dict[str, Any]) -> None:
        """Route an inbound JSON-RPC message to the waiting future or notification handlers."""
        msg_id = msg.get("id")
        if msg_id is not None:
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(
                        RuntimeError(
                            f"MCP error: {msg['error'].get('message', msg['error'])}"
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # Notification — no id field
        method = msg.get("method", "")
        params = msg.get("params", {})
        for handler in self._notification_handlers:
            try:
                result = handler(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug(
                    "McpWsClient[%s]: notification handler error: %s", self.name, exc
                )

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
        """Send a JSON-RPC request over WebSocket and await the response."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError(f"McpWsClient[{self.name}]: not connected")

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
            await self._ws.send_str(json.dumps(payload))
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"McpWsClient[{self.name}]: send failed: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(
                f"McpWsClient[{self.name}]: no response for '{method}' in {timeout}s"
            )

    async def _send_notification(
        self, method: str, params: Optional[Dict] = None
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_str(
                json.dumps(
                    {
                        "jsonrpc": _JSONRPC_VERSION,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
        except Exception:
            pass

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
        logger.debug("McpWsClient[%s]: initialize result: %s", self.name, result)
        await self._send_notification("notifications/initialized")

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

            def _make_fn(c: "McpWsClient", tn: str):
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
                    "McpWsClient[%s]: failed to register tool %s: %s",
                    self.name,
                    _name,
                    exc,
                )
        return count
