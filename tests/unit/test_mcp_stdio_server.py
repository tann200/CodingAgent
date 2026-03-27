"""Tests for GAP 3: MCP STDIO Server."""

import pytest
import json
from src.core.orchestration.mcp_stdio_server import (
    MCPStdioServer,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcNotification,
)


class TestJsonRpcParsing:
    """Tests for JSON-RPC message parsing."""

    def test_parse_valid_request(self):
        """Test parsing a valid JSON-RPC request."""
        server = MCPStdioServer()
        line = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}'

        result = server._parse_json_rpc(line)

        assert isinstance(result, JsonRpcRequest)
        assert result.method == "initialize"
        assert result.id == 1
        assert result.params == {}

    def test_parse_valid_notification(self):
        """Test parsing a valid JSON-RPC notification (no id)."""
        server = MCPStdioServer()
        line = '{"jsonrpc": "2.0", "method": "tool.execute.start", "params": {"data": "test"}}'

        result = server._parse_json_rpc(line)

        assert isinstance(result, JsonRpcNotification)
        assert result.method == "tool.execute.start"
        assert result.params == {"data": "test"}

    def test_parse_invalid_json(self):
        """Test parsing invalid JSON returns None."""
        server = MCPStdioServer()
        line = "not valid json"

        result = server._parse_json_rpc(line)

        assert result is None

    def test_parse_invalid_version(self):
        """Test parsing JSON with wrong version returns None."""
        server = MCPStdioServer()
        line = '{"jsonrpc": "1.0", "method": "test"}'

        result = server._parse_json_rpc(line)

        assert result is None


class TestJsonRpcResponse:
    """Tests for JSON-RPC response building."""

    def test_build_response(self):
        """Test building a JSON-RPC response."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="test", params={})

        result = server._build_response(request, {"status": "ok"})

        parsed = json.loads(result)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == 1
        assert parsed["result"]["status"] == "ok"

    def test_build_error_response(self):
        """Test building a JSON-RPC error response."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="test", params={})

        result = server._build_error_response(request, -32601, "Method not found")

        parsed = json.loads(result)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == 1
        assert parsed["error"]["code"] == -32601
        assert parsed["error"]["message"] == "Method not found"

    def test_build_notification(self):
        """Test building a JSON-RPC notification."""
        server = MCPStdioServer()

        result = server._build_notification("session.update", {"state": "updated"})

        parsed = json.loads(result)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "session.update"
        assert parsed["params"]["state"] == "updated"


class TestMCPMethodHandlers:
    """Tests for MCP method handlers."""

    def test_initialize_method(self):
        """Test initialize method returns capabilities."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="initialize", params={})

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["result"]["protocolVersion"] == "1.0"
        assert "capabilities" in parsed["result"]
        assert parsed["result"]["capabilities"]["tools"] is True

    def test_ping_method(self):
        """Test ping method returns pong."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="ping", params={})

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["result"]["pong"] is True

    def test_tools_list_method(self):
        """Test tools/list method returns tools list."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="tools/list", params={})

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert "tools" in parsed["result"]
        assert isinstance(parsed["result"]["tools"], list)

    def test_tools_call_method(self):
        """Test tools/call method."""
        server = MCPStdioServer()
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id=1,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "test.py"}},
        )

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["result"]["status"] == "executing"

    def test_session_request_state_method(self):
        """Test session/request_state method."""
        server = MCPStdioServer()
        request = JsonRpcRequest(
            jsonrpc="2.0", id=1, method="session/request_state", params={}
        )

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["result"]["status"] == "requested"

    def test_resources_list_method(self):
        """Test resources/list method."""
        server = MCPStdioServer()
        request = JsonRpcRequest(
            jsonrpc="2.0", id=1, method="resources/list", params={}
        )

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert "resources" in parsed["result"]

    def test_prompts_list_method(self):
        """Test prompts/list method."""
        server = MCPStdioServer()
        request = JsonRpcRequest(jsonrpc="2.0", id=1, method="prompts/list", params={})

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert "prompts" in parsed["result"]

    def test_unknown_method(self):
        """Test unknown method returns error."""
        server = MCPStdioServer()
        request = JsonRpcRequest(
            jsonrpc="2.0", id=1, method="unknown_method", params={}
        )

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["error"]["code"] == -32601
        assert "Method not found" in parsed["error"]["message"]

    def test_logging_set_level_method(self):
        """Test logging/setLevel method."""
        server = MCPStdioServer()
        request = JsonRpcRequest(
            jsonrpc="2.0", id=1, method="logging/setLevel", params={"level": "debug"}
        )

        result = server._handle_request(request)

        parsed = json.loads(result)
        assert parsed["result"]["status"] == "ok"


class TestMCPEventBusSubscription:
    """Tests for EventBus subscription functionality."""

    def test_subscribe_to_event_bus(self):
        """Test server subscribes to required topics."""
        from src.core.orchestration.event_bus import EventBus

        bus = EventBus()
        server = MCPStdioServer()
        server._event_bus = bus

        # Manually subscribe (normally done in run())
        server._subscribe_to_event_bus()

        # Verify key topics are subscribed
        assert "tool.execute.start" in bus._subscribers
        assert "tool.execute.finish" in bus._subscribers
        assert "tool.execute.error" in bus._subscribers
        assert "plan.progress" in bus._subscribers
        assert "session.hydrated" in bus._subscribers
        assert "file.modified" in bus._subscribers

    def test_event_bus_wildcard_handler(self):
        """Test EventBus wildcard handler forwards events."""
        from src.core.orchestration.event_bus import EventBus

        bus = EventBus()
        server = MCPStdioServer()
        server._event_bus = bus
        server._subscribe_to_event_bus()

        # Publish a tool event
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_123",
                "title": "read_file",
                "status": "in_progress",
            },
        )

        # The handler should not raise an exception
        # (actual stdout output is tested separately)
