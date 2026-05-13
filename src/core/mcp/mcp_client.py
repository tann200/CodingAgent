"""mcp_client.py — Outbound MCP client (stdio transport).

TASK-21 / S3-A: Connects to an MCP server subprocess over stdio using the
Model Context Protocol JSON-RPC 2.0 wire format.

This client handles the stdio transport only (the first transport in the
dependency plan).  HTTP/SSE transport is deferred to S3-A-http.

Usage::

    from src.core.mcp.mcp_client import McpStdioClient

    client = McpStdioClient(
        name="filesystem",
        cmd=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/tmp/hello.txt"})
    await client.disconnect()

Integration with ToolRegistry::

    from src.core.mcp.mcp_client import McpStdioClient
    from src.tools._registry import build_registry

    client = McpStdioClient(name="mymcp", cmd=["my-mcp-server"])
    await client.connect()
    registry = build_registry()
    await client.register_tools(registry)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_INITIALIZE_TIMEOUT = 10.0
_CALL_TIMEOUT = 30.0


@dataclass
class McpToolDefinition:
    """Describes a tool exposed by an MCP server."""

    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    server_name: str = ""


@dataclass
class McpToolResult:
    """Result from calling an MCP server tool."""

    ok: bool
    content: Any = None
    error: str = ""


class McpStdioClient:
    """Async MCP client using stdio transport.

    Lifecycle:
        1. ``await client.connect()``  — launch subprocess, run ``initialize``
        2. ``await client.list_tools()`` — enumerate available tools
        3. ``await client.call_tool(name, args)`` — invoke a tool
        4. ``await client.disconnect()`` — send ``shutdown`` and terminate

    Attributes:
        name:    Human-readable server name (used as tool origin tag).
        cmd:     Command to launch the MCP server subprocess.
        tools:   Tool definitions discovered after ``connect()``.
    """

    def __init__(
        self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None
    ) -> None:
        self.name = name
        self.cmd = cmd
        # Optional environment variables to inject into the subprocess.
        # When provided we merge these with the current process environment
        # before launching so sensitive keys (eg. CONTEXT7_API_KEY) can be
        # supplied without committing them to repository files.
        self._env = dict(env) if isinstance(env, dict) else None
        self.tools: List[McpToolDefinition] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._connected: bool = False
        self._notification_handlers: List[
            Callable[[str, Dict[str, Any]], Optional[Awaitable[None]]]
        ] = []

    def __del__(self) -> None:
        """Safety net: kill the subprocess if disconnect() was not called."""
        if self._process is not None and self._process.returncode is None:
            try:
                os.kill(self._process.pid, signal.SIGTERM)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

    def add_notification_handler(
        self,
        handler: Callable[[str, Dict[str, Any]], Optional[Awaitable[None]]],
    ) -> None:
        """Register a callback for inbound server notifications.

        The handler receives ``(method, params)`` for JSON-RPC notifications
        where the server message has a ``method`` field and no ``id``.
        """
        if not callable(handler):
            raise TypeError("notification handler must be callable")
        self._notification_handlers.append(handler)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Start the MCP server subprocess and run ``initialize``."""
        if self._connected:
            return

        logger.info("mcp_client[%s]: starting subprocess %s", self.name, self.cmd)
        # Merge provided env onto the current os.environ to avoid dropping
        # necessary system variables. If no env overrides were provided the
        # subprocess inherits the parent environment unchanged.
        proc_env = None
        if self._env is not None:
            proc_env = os.environ.copy()
            proc_env.update({str(k): str(v) for k, v in self._env.items()})

        self._process = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )

        # Start background reader task
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-reader-{self.name}"
        )

        # Run MCP initialize handshake — clean up on failure so the client
        # does not hold a dangling process or reader task.
        try:
            await self._initialize()
        except Exception as exc:
            logger.warning(
                "mcp_client[%s]: initialize failed: %s — cleaning up", self.name, exc
            )
            if self._reader_task:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._reader_task = None
            if self._process:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
            raise
        self._connected = True
        logger.info("mcp_client[%s]: connected", self.name)

    async def disconnect(self) -> None:
        """Send ``shutdown`` notification and terminate the subprocess."""
        if not self._connected:
            return
        try:
            await self._notify("shutdown", {})
        except Exception:
            pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._connected = False
        logger.info("mcp_client[%s]: disconnected", self.name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> List[McpToolDefinition]:
        """Return the list of tools exposed by this MCP server."""
        resp = await self._request("tools/list", {})
        raw_tools = resp.get("tools", [])
        self.tools = [
            McpToolDefinition(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            for t in raw_tools
            if t.get("name")
        ]
        return self.tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> McpToolResult:
        """Invoke *name* on the MCP server with *arguments*.

        Returns a ``McpToolResult`` with ``ok=True`` on success.
        """
        try:
            resp = await asyncio.wait_for(
                self._request("tools/call", {"name": name, "arguments": arguments}),
                timeout=_CALL_TIMEOUT,
            )
            # MCP spec: result has a "content" list of content blocks
            content = resp.get("content", resp)
            # Flatten text blocks to a single string for convenience
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text") or block.get("data") or ""))
                    else:
                        parts.append(str(block))
                content = "\n".join(p for p in parts if p)
            return McpToolResult(ok=True, content=content)
        except asyncio.TimeoutError:
            return McpToolResult(ok=False, error=f"Tool call '{name}' timed out")
        except Exception as exc:
            return McpToolResult(ok=False, error=str(exc))

    async def register_tools(self, registry) -> int:
        """Discover tools and register them in *registry*.

        Tools are registered with ``origin="mcp"`` and a synthetic function
        that calls ``call_tool()`` synchronously via a new event loop.

        Returns:
            Number of tools registered.
        """
        tools = await self.list_tools()
        count = 0
        for tool_def in tools:
            tool_name = (
                f"{self.name}__{tool_def.name}"
                if "__" not in tool_def.name
                else tool_def.name
            )
            client = self  # capture for closure

            async def _async_call(
                _tool_def=tool_def, _client=client, **kwargs
            ) -> Dict[str, Any]:
                result = await _client.call_tool(_tool_def.name, kwargs)
                if result.ok:
                    return {"ok": True, "output": result.content}
                return {"ok": False, "error": result.error}

            try:
                try:
                    # Preferred path for src.tools._registry.ToolRegistry
                    registry.register(
                        name=tool_name,
                        fn=_async_call,
                        description=tool_def.description,
                        schema=tool_def.input_schema,
                        origin="mcp",
                    )
                except TypeError:
                    # Backward-compat path for orchestration ToolRegistry
                    registry.register(
                        name=tool_name,
                        fn=_async_call,
                        description=tool_def.description,
                    )
                count += 1
            except Exception as exc:
                logger.warning(
                    "mcp_client[%s]: failed to register %s: %s",
                    self.name,
                    tool_name,
                    exc,
                )

        logger.info("mcp_client[%s]: registered %d tool(s)", self.name, count)
        return count

    # ------------------------------------------------------------------
    # Internal JSON-RPC helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, message: Dict[str, Any]) -> None:
        """Write a JSON-RPC message to the subprocess stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP client not connected")
        raw = json.dumps(message) + "\n"
        self._process.stdin.write(raw.encode())
        await self._process.stdin.drain()

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        req_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=_INITIALIZE_TIMEOUT)
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
            "params": params,
        }
        await self._send(msg)

    async def _initialize(self) -> None:
        """Run the MCP initialize handshake."""
        # NOTE: _request() already enforces _INITIALIZE_TIMEOUT internally;
        # wrapping it in a second wait_for would cause the inner CancelledError
        # to propagate before the outer error handler runs.
        resp = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "codingagent", "version": "1.0"},
            },
        )
        # Send initialized notification
        await self._notify("notifications/initialized", {})
        logger.debug(
            "mcp_client[%s]: initialize complete: %s", self.name, resp.get("serverInfo")
        )

    async def _read_loop(self) -> None:
        """Background task: read stdout lines and dispatch JSON-RPC responses."""
        if not self._process or not self._process.stdout:
            return
        try:
            async for line in self._process.stdout:
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "mcp_client[%s]: non-JSON stdout: %s", self.name, line[:120]
                    )
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending[msg_id]
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result", {}))
                else:
                    method = msg.get("method")
                    if method and isinstance(method, str):
                        params = msg.get("params")
                        if not isinstance(params, dict):
                            params = {}
                        for handler in list(self._notification_handlers):
                            try:
                                maybe_coro = handler(method, params)
                                if asyncio.iscoroutine(maybe_coro):
                                    await maybe_coro
                            except Exception as notify_exc:
                                logger.debug(
                                    "mcp_client[%s]: notification handler error (%s): %s",
                                    self.name,
                                    method,
                                    notify_exc,
                                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("mcp_client[%s]: read loop error: %s", self.name, exc)
        finally:
            # Resolve any remaining pending futures with an error
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP client disconnected"))
            self._pending.clear()


# ---------------------------------------------------------------------------
# TASK-9 / TASK-10: Multi-transport factory
# ---------------------------------------------------------------------------


def create_mcp_client(
    name: str,
    *,
    cmd: Optional[List[str]] = None,
    url: Optional[str] = None,
    headers: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Any:
    """Return the appropriate MCP client for the given transport.

    Transport selection:
    - If *cmd* is provided → stdio transport (``McpStdioClient``).
    - If *url* starts with ``ws://`` or ``wss://`` → WebSocket transport
      (``McpWsClient``).
    - If *url* starts with ``http://`` or ``https://`` → SSE transport
      (``McpSseClient``).

    Parameters
    ----------
    name:
        Human-readable server name used as tool origin tag.
    cmd:
        Command to launch the MCP server subprocess (stdio transport).
    url:
        URL of the MCP server (SSE or WebSocket transport).
    headers:
        Optional extra HTTP headers (SSE / WebSocket only).

    Returns
    -------
    McpStdioClient | McpSseClient | McpWsClient

    Raises
    ------
    ValueError
        When neither *cmd* nor *url* is provided, or when the URL scheme is
        not recognised.

    Examples
    --------
    ::

        # stdio
        client = create_mcp_client("fs", cmd=["npx", "@mcp/server-filesystem", "/tmp"])

        # SSE
        client = create_mcp_client("remote", url="http://localhost:3000")

        # WebSocket
        client = create_mcp_client("ws_server", url="ws://localhost:3000/ws")
    """
    if cmd is not None:
        return McpStdioClient(name=name, cmd=cmd, env=env)

    if url is not None:
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme in ("ws", "wss"):
            from src.core.mcp.mcp_ws_client import McpWsClient

            return McpWsClient(name=name, url=url, headers=headers)
        if scheme in ("http", "https"):
            from src.core.mcp.mcp_sse_client import McpSseClient

            return McpSseClient(name=name, url=url, headers=headers)
        raise ValueError(
            f"create_mcp_client: unrecognised URL scheme {scheme!r} in {url!r}. "
            "Expected http://, https://, ws://, or wss://."
        )

    raise ValueError(
        "create_mcp_client: provide either 'cmd' (stdio) or 'url' (SSE/WebSocket)."
    )
