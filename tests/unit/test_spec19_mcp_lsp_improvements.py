"""Unit tests for SPEC-19 MCP and LSP improvements."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.core.mcp.mcp_http_client import McpHttpClient
from src.core.mcp.mcp_sse_client import McpSseClient
from src.core.mcp.mcp_ws_client import McpWsClient
from src.core.mcp.mcp_client import McpStdioClient, McpToolDefinition
from src.core.indexing.lsp_client import LSPClient


@pytest.mark.asyncio
async def test_mcp_http_client_connect_and_result_unwrapping():
    """Verify McpHttpClient sends initialized notification and unwraps JSON-RPC result."""
    client = McpHttpClient(name="testhttp", url="http://localhost:3000/mcp")

    mock_client = AsyncMock()
    # Response for initialize
    resp_init = MagicMock()
    resp_init.headers = {"Content-Type": "application/json"}
    resp_init.json.return_value = {
        "jsonrpc": "2.0",
        "id": "testhttp-initialize-1",
        "result": {"protocolVersion": "2024-11-05"},
    }
    # Response for notifications/initialized
    resp_notify = MagicMock()
    resp_notify.headers = {"Content-Type": "application/json"}
    resp_notify.json.return_value = {}

    # Response for tools/list
    resp_tools = MagicMock()
    resp_tools.headers = {"Content-Type": "application/json"}
    resp_tools.json.return_value = {
        "jsonrpc": "2.0",
        "id": "testhttp-tools/list-2",
        "result": {
            "tools": [
                {
                    "name": "search",
                    "description": "Search docs",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                }
            ]
        },
    }

    mock_client.post.side_effect = [resp_init, resp_notify, resp_tools, resp_tools]

    with patch("httpx.AsyncClient", return_value=mock_client):
        await client.connect()
        assert client.is_connected
        # Check that post was called twice: initialize and notifications/initialized
        assert mock_client.post.call_count == 2
        notify_call = mock_client.post.call_args_list[1]
        assert notify_call.kwargs["json"]["method"] == "notifications/initialized"

        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "search"
        assert tools[0].input_schema["properties"]["q"]["type"] == "string"

        # Register tools passes schema to registry
        registry = MagicMock()
        registered = await client.register_tools(registry)
        assert registered == 1
        registry.register.assert_called_once()
        _, reg_kwargs = registry.register.call_args
        assert reg_kwargs["schema"] == {"type": "object", "properties": {"q": {"type": "string"}}}

        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_http_client_error_handling():
    """Verify McpHttpClient raises RuntimeError when JSON-RPC returns error."""
    client = McpHttpClient(name="testhttp", url="http://localhost:3000/mcp")
    client._client = AsyncMock()
    client._connected = True

    resp_error = MagicMock()
    resp_error.headers = {"Content-Type": "application/json"}
    resp_error.json.return_value = {
        "jsonrpc": "2.0",
        "id": "testhttp-tools/call-1",
        "error": {"code": -32600, "message": "Invalid params"},
    }
    client._client.post.return_value = resp_error

    result = await client.call_tool("search", {})
    assert not result.ok
    assert "Invalid params" in result.error


@pytest.mark.asyncio
async def test_mcp_sse_and_ws_client_register_tools_schema():
    """Verify McpSseClient and McpWsClient pass schema=tool_defn.input_schema to registry."""
    tool_def = McpToolDefinition(
        name="do_work",
        description="Does work",
        input_schema={"type": "object", "required": ["task"]},
        server_name="testserver",
    )

    sse_client = McpSseClient(name="testserver", url="http://localhost:8000/sse")
    with patch.object(sse_client, "list_tools", new_callable=AsyncMock, return_value=[tool_def]):
        reg_sse = MagicMock()
        count = await sse_client.register_tools(reg_sse)
        assert count == 1
        reg_sse.register.assert_called_once()
        assert reg_sse.register.call_args.kwargs["schema"] == tool_def.input_schema

    ws_client = McpWsClient(name="testserver", url="ws://localhost:8000/ws")
    with patch.object(ws_client, "list_tools", new_callable=AsyncMock, return_value=[tool_def]):
        reg_ws = MagicMock()
        count = await ws_client.register_tools(reg_ws)
        assert count == 1
        reg_ws.register.assert_called_once()
        assert reg_ws.register.call_args.kwargs["schema"] == tool_def.input_schema


@pytest.mark.asyncio
async def test_mcp_stdio_buffer_limit_and_stderr_drain():
    """Verify McpStdioClient uses 16MiB buffer limit and starts stderr drain task."""
    client = McpStdioClient(name="stdio_test", cmd=["echo", "hi"])

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    async def _mock_readline():
        return b""

    mock_proc.stderr.readline = _mock_readline
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec, \
         patch.object(client, "_initialize", new_callable=AsyncMock):
        await client.connect()
        mock_exec.assert_called_once()
        assert mock_exec.call_args.kwargs["limit"] == 16 * 1024 * 1024
        assert client._stderr_task is not None

        await client.disconnect()
        assert client._stderr_task is None


@pytest.mark.asyncio
async def test_lsp_client_init_timeout_terminates_subprocess(tmp_path: Path):
    """Verify LSPClient terminates child process when initialize times out."""
    client = LSPClient(server_cmd=["fake-lsp"], workspace_root=tmp_path)

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc), \
         patch.object(client, "_request", new_callable=AsyncMock, return_value=None):
        await client.start()
        # Initialize timed out (returned None)
        assert not client.available
        assert client._proc is None
        mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_lsp_client_did_open_and_rename_document_changes(tmp_path: Path):
    """Verify LSPClient sends textDocument/didOpen and parses documentChanges in rename."""
    test_file = tmp_path / "hello.py"
    test_file.write_text("def hello(): pass\n", encoding="utf-8")
    uri = test_file.as_uri()

    client = LSPClient(server_cmd=["fake-lsp"], workspace_root=tmp_path)
    client._started = True
    client._proc = MagicMock()
    client._proc.returncode = None

    with patch.object(client, "_notify", new_callable=AsyncMock) as mock_notify, \
         patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {
            "documentChanges": [
                {
                    "textDocument": {"uri": uri, "version": 1},
                    "edits": [{"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}, "newText": "greeting"}],
                }
            ]
        }

        edit = await client.rename(uri, 0, 4, "greeting")
        assert uri in edit.changes
        assert edit.changes[uri][0]["newText"] == "greeting"

        # didOpen should have been notified
        assert uri in client._open_documents
        did_open_calls = [
            call for call in mock_notify.call_args_list if call.args[0] == "textDocument/didOpen"
        ]
        assert len(did_open_calls) == 1
        assert did_open_calls[0].args[1]["textDocument"]["uri"] == uri
        assert did_open_calls[0].args[1]["textDocument"]["languageId"] == "python"

        await client.shutdown()
        assert len(client._open_documents) == 0
