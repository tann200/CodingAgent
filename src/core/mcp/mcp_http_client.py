"""mcp_http_client.py — Outbound MCP client (HTTP Streamable transport).

Implements the HTTP Streamable transport for connecting to MCP servers
over HTTP using the Model Context Protocol.

Usage::

    from src.core.mcp.mcp_http_client import McpHttpClient

    client = McpHttpClient(
        name="my-server",
        url="http://localhost:3000/mcp",
    )
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("my_tool", {"arg": "value"})
    await client.disconnect()

Integration with ToolRegistry::

    from src.core.mcp.mcp_http_client import McpHttpClient
    from src.core.orchestration.tool_registry import ToolRegistry

    client = McpHttpClient(name="mymcp", url="http://localhost:3000/mcp")
    await client.connect()
    registry = ToolRegistry()
    await client.register_tools(registry)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from src.core.mcp.mcp_client import McpToolDefinition, McpToolResult

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_INITIALIZE_TIMEOUT = 10.0
_CALL_TIMEOUT = 30.0
_LIST_TOOLS_TIMEOUT = 30.0


class McpHttpClient:
    """Async MCP client using HTTP Streamable transport.

    Lifecycle:
        - Created (not connected)
        - connect() -> Connected
        - call_tool() / list_tools() -> Active
        - disconnect() -> Disconnected
    """

    def __init__(
        self,
        name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = _CALL_TIMEOUT,
    ):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._info: Optional[Dict[str, Any]] = None
        self._tools: List[McpToolDefinition] = []
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the MCP server and initialize."""
        if self._connected:
            return

        self._client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.headers,
            },
            timeout=httpx.Timeout(self.timeout),
        )

        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": f"codingagent-mcp-{self.name}",
                        "version": "0.1.0",
                    },
                },
            )
            self._connected = True
            logger.info(f"McpHttpClient: connected to {self.name} at {self.url}")
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
        logger.info(f"McpHttpClient: disconnected from {self.name}")

    async def list_tools(self) -> List[McpToolDefinition]:
        """Return the list of tools exposed by this MCP server."""
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")

        resp = await self._request("tools/list", {})
        raw_tools = resp.get("tools", [])
        self._tools = [
            McpToolDefinition(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            for t in raw_tools
            if t.get("name")
        ]
        return self._tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> McpToolResult:
        """Invoke *name* on the MCP server with *arguments*."""
        if not self._connected:
            return McpToolResult(ok=False, error="Not connected")

        try:
            resp = await asyncio.wait_for(
                self._request("tools/call", {"name": name, "arguments": arguments}),
                timeout=self.timeout,
            )
            content = resp.get("content", resp)
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text") or block.get("data") or ""))
                    else:
                        parts.append(str(block))
                return McpToolResult(ok=True, content="\n".join(parts))
            return McpToolResult(ok=True, content=content)
        except asyncio.TimeoutError:
            return McpToolResult(ok=False, error=f"Tool '{name}' timed out")
        except Exception as exc:
            return McpToolResult(ok=False, error=str(exc))

    async def register_tools(self, registry) -> int:
        """Register MCP tools into the tool registry.

        Tools are registered as ``{server_name}/{tool_name}`` to avoid
        collisions with local tools.

        Returns the number of tools registered.
        """
        tools = await self.list_tools()
        count = 0
        for tool_def in tools:
            tool_name = f"{self.name}/{tool_def.name}"

            async def _call(tool_def=tool_def, **kwargs):
                result = await self.call_tool(tool_def.name, kwargs)
                if result.ok:
                    return result.content
                raise RuntimeError(result.error)

            try:
                registry.register(
                    name=tool_name,
                    fn=_call,
                    description=tool_def.description,
                    origin="mcp",
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to register {tool_name}: {e}")

        logger.info(f"McpHttpClient: registered {count} tools from {self.name}")
        return count

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request to the MCP server."""
        if not self._client:
            raise RuntimeError("No client. Call connect() first.")

        request_id = f"{self.name}-{method}"
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }

        try:
            response = await self._client.post(self.url, json=payload)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")

            if "text/event-stream" in content_type:
                return await self._handle_sse(response)
            else:
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"McpHttpClient: request failed: {e}")
            raise

    async def _handle_sse(self, response: httpx.Response) -> Dict[str, Any]:
        """Handle SSE stream response."""
        result = {"content": [], "tools": []}
        try:
            for line in response.text.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    data = line[6:]
                    try:
                        event = json.loads(data)
                        if event.get("method") == "tools/list":
                            result["tools"] = event.get("params", {}).get("tools", [])
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.debug(f"SSE parsing: {e}")
        return result
