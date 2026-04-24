"""tests/unit/test_mcp_client.py — Tests for TASK-21 / S3-A McpStdioClient.

Uses asyncio pipes to simulate an MCP server without launching a real subprocess.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock

from src.core.mcp.mcp_client import McpStdioClient, McpToolDefinition, McpToolResult


class TestMcpToolDefinition:
    def test_construction(self):
        t = McpToolDefinition(name="read_file", description="Reads a file")
        assert t.name == "read_file"
        assert t.server_name == ""

    def test_with_server_name(self):
        t = McpToolDefinition(name="foo", description="bar", server_name="myserver")
        assert t.server_name == "myserver"


class TestMcpToolResult:
    def test_ok_result(self):
        r = McpToolResult(ok=True, content="hello")
        assert r.ok is True
        assert r.error == ""

    def test_error_result(self):
        r = McpToolResult(ok=False, error="timeout")
        assert r.ok is False
        assert r.content is None


class TestMcpStdioClientInit:
    def test_construction(self):
        client = McpStdioClient(name="test", cmd=["echo", "hello"])
        assert client.name == "test"
        assert client.cmd == ["echo", "hello"]
        assert client._connected is False
        assert client.tools == []

    def test_next_id_increments(self):
        client = McpStdioClient(name="t", cmd=["x"])
        assert client._next_id() == 1
        assert client._next_id() == 2
        assert client._next_id() == 3


class TestMcpClientCallTool:
    """Tests for call_tool without a real subprocess (mock the request)."""

    @pytest.mark.asyncio
    async def test_call_tool_ok(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _mock_request(method, params):
            assert method == "tools/call"
            assert params["name"] == "read_file"
            return {"content": [{"text": "file contents"}]}

        client._request = _mock_request
        result = await client.call_tool("read_file", {"path": "foo.txt"})
        assert result.ok is True
        assert "file contents" in result.content

    @pytest.mark.asyncio
    async def test_call_tool_timeout(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _slow_request(method, params):
            await asyncio.sleep(100)

        client._request = _slow_request  # type: ignore[method-assign]
        # Monkeypatch the timeout to be very short
        import src.core.mcp.mcp_client as mod

        original = mod._CALL_TIMEOUT
        mod._CALL_TIMEOUT = 0.01
        try:
            result = await client.call_tool("slow_tool", {})
            assert result.ok is False
            assert "timed out" in result.error
        finally:
            mod._CALL_TIMEOUT = original

    @pytest.mark.asyncio
    async def test_call_tool_exception(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _err_request(method, params):
            raise ValueError("bad tool")

        client._request = _err_request
        result = await client.call_tool("broken", {})
        assert result.ok is False
        assert "bad tool" in result.error

    @pytest.mark.asyncio
    async def test_call_tool_dict_content_passthrough(self):
        """Result where content is a plain dict (not list) passes through."""
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _mock_request(method, params):
            return {"content": {"key": "value"}}

        client._request = _mock_request
        result = await client.call_tool("dict_tool", {})
        assert result.ok is True
        assert result.content == {"key": "value"}


class TestMcpClientListTools:
    @pytest.mark.asyncio
    async def test_list_tools_populated(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _mock_request(method, params):
            return {
                "tools": [
                    {"name": "tool_a", "description": "desc A", "inputSchema": {}},
                    {"name": "tool_b", "description": "desc B"},
                ]
            }

        client._request = _mock_request
        tools = await client.list_tools()
        assert len(tools) == 2
        assert tools[0].name == "tool_a"
        assert tools[1].name == "tool_b"
        assert tools[0].server_name == "srv"

    @pytest.mark.asyncio
    async def test_list_tools_empty(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        client._connected = True

        async def _mock_request(method, params):
            return {"tools": []}

        client._request = _mock_request
        tools = await client.list_tools()
        assert tools == []


class TestMcpClientRegisterTools:
    @pytest.mark.asyncio
    async def test_register_tools_calls_registry(self):
        client = McpStdioClient(name="myserver", cmd=["dummy"])
        client._connected = True
        client.tools = []

        async def _mock_list():
            return [
                McpToolDefinition(
                    name="read", description="read it", server_name="myserver"
                ),
                McpToolDefinition(
                    name="write", description="write it", server_name="myserver"
                ),
            ]

        client.list_tools = _mock_list

        registry = MagicMock()
        registry.register = MagicMock()

        count = await client.register_tools(registry)
        assert count == 2
        assert registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_register_tools_skips_conflict(self):
        client = McpStdioClient(name="myserver", cmd=["dummy"])
        client._connected = True

        async def _mock_list():
            return [McpToolDefinition(name="conflict", description="x")]

        client.list_tools = _mock_list

        registry = MagicMock()
        registry.register = MagicMock(side_effect=ValueError("conflict"))

        count = await client.register_tools(registry)
        assert count == 0  # conflict swallowed, not raised


class TestMcpClientDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected_is_noop(self):
        client = McpStdioClient(name="srv", cmd=["dummy"])
        # Should not raise
        await client.disconnect()
        assert client._connected is False
