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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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

    def __init__(self, name: str, cmd: List[str]) -> None:
        self.name = name
        self.cmd = cmd
        self.tools: List[McpToolDefinition] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._connected: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Start the MCP server subprocess and run ``initialize``."""
        if self._connected:
            return

        logger.info("mcp_client[%s]: starting subprocess %s", self.name, self.cmd)
        self._process = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                registry.register(
                    name=tool_name,
                    fn=_async_call,
                    description=tool_def.description,
                    schema=tool_def.input_schema,
                    origin="mcp",
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
