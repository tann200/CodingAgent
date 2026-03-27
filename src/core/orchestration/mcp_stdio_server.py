"""
MCP STDIO Server - Bridges EventBus to standard input/output for IDE integration.

This server acts as a bridge between:
- Incoming JSON-RPC commands from an IDE (stdin)
- The CodingAgent EventBus (internal Python events)
- Outgoing JSON-RPC notifications to the IDE (stdout)

GAP 3: Implements the I/O boundary for external IDE communication.
Supports ACP (Agent Client Protocol) and MCP (Model Context Protocol) patterns.
"""

import asyncio
import json
import logging
import sys
import threading
from typing import Any, Dict, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class JsonRpcVersion(Enum):
    """JSON-RPC version identifier."""

    V2_0 = "2.0"


@dataclass
class JsonRpcRequest:
    """Represents a JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JsonRpcResponse:
    """Represents a JSON-RPC 2.0 response."""

    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


@dataclass
class JsonRpcNotification:
    """Represents a JSON-RPC 2.0 notification (no response expected)."""

    jsonrpc: str = "2.0"
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


class MCPStdioServer:
    """
    MCP/ACP-compatible STDIO server that bridges IDE commands to EventBus.

    Listens for JSON-RPC commands on stdin and:
    - Converts them to EventBus events
    - Subscribes to EventBus wildcard and outputs JSON-RPC notifications on stdout

    Step 9: Pass orchestrator=<Orchestrator> to enable tools/list to return real tool names.
    """

    def __init__(self, orchestrator=None):
        self._running = False
        self._event_bus = None
        self._subscription_id = 0
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._lock = threading.Lock()
        # Optional orchestrator reference for tools/list and resources/read
        self._orchestrator = orchestrator

    def _get_event_bus(self):
        """Lazy-load EventBus to avoid circular imports."""
        if self._event_bus is None:
            try:
                from src.core.orchestration.event_bus import get_event_bus

                self._event_bus = get_event_bus()
            except Exception as e:
                logger.error(f"Failed to get EventBus: {e}")
        return self._event_bus

    def _parse_json_rpc(self, line: str) -> Optional[JsonRpcRequest]:
        """Parse a JSON-RPC message from string."""
        try:
            data = json.loads(line.strip())
            if not isinstance(data, dict):
                return None

            # Check JSON-RPC version
            if data.get("jsonrpc") != "2.0":
                return None

            # Request (has id) or Notification (no id)
            if "id" in data:
                return JsonRpcRequest(
                    jsonrpc=data.get("jsonrpc", "2.0"),
                    id=data.get("id"),
                    method=data.get("method", ""),
                    params=data.get("params", {}),
                )
            else:
                return JsonRpcNotification(
                    jsonrpc=data.get("jsonrpc", "2.0"),
                    method=data.get("method", ""),
                    params=data.get("params", {}),
                )
        except json.JSONDecodeError:
            return None

    def _build_response(self, request: JsonRpcRequest, result: Any) -> str:
        """Build a JSON-RPC response string."""
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id=request.id,
            result=result,
        )
        return json.dumps(response.__dict__)

    def _build_error_response(
        self, request: JsonRpcRequest, code: int, message: str
    ) -> str:
        """Build a JSON-RPC error response string."""
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id=request.id,
            error={"code": code, "message": message},
        )
        return json.dumps(response.__dict__)

    def _build_notification(self, method: str, params: Dict[str, Any]) -> str:
        """Build a JSON-RPC notification string."""
        notification = JsonRpcNotification(
            jsonrpc="2.0",
            method=method,
            params=params,
        )
        return json.dumps(notification.__dict__)

    def _handle_request(self, request: JsonRpcRequest) -> Optional[str]:
        """Handle an incoming JSON-RPC request."""
        method = request.method
        params = request.params

        # ACP/MCP method handlers
        if method == "initialize":
            # Initialize the connection - return capabilities
            result = {
                "protocolVersion": "1.0",
                "capabilities": {
                    "tools": True,
                    "resources": True,
                    "prompts": True,
                },
                "serverInfo": {
                    "name": "coding-agent",
                    "version": "1.0.0",
                },
            }
            return self._build_response(request, result)

        elif method == "session/request_state":
            # GAP 1: Forward state request to EventBus
            eb = self._get_event_bus()
            if eb:
                eb.publish("session.request_state", params)
            # Return ack - actual state comes via notification
            return self._build_response(request, {"status": "requested"})

        elif method == "tools/list":
            # Return available tools from the live tool registry when orchestrator is set.
            tools_list = []
            if self._orchestrator is not None:
                try:
                    registry = getattr(self._orchestrator, "tool_registry", None)
                    if registry and hasattr(registry, "tools"):
                        tools_list = [
                            {"name": name, "description": getattr(t, "description", "")}
                            for name, t in registry.tools.items()
                        ]
                except Exception as _te:
                    logger.debug(f"MCPStdioServer: tools/list error: {_te}")
            result = {"tools": tools_list}
            return self._build_response(request, result)

        elif method == "tools/call":
            # P4-2: Execute tool synchronously via orchestrator when available;
            # fall back to EventBus fire-and-forget when orchestrator is absent.
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            if self._orchestrator is not None:
                try:
                    tool_result = self._orchestrator.execute_tool(
                        {"name": tool_name, "arguments": tool_args}
                    )
                    return self._build_response(
                        request,
                        {
                            "content": [{"type": "text", "text": json.dumps(tool_result)}],
                            "isError": not tool_result.get("ok", True),
                        },
                    )
                except Exception as _te:
                    return self._build_error_response(
                        request, -32603, f"Tool execution error: {_te}"
                    )
            # No orchestrator — fire via EventBus (async path)
            eb = self._get_event_bus()
            if eb:
                eb.publish(
                    "mcp.tool_call",
                    {"tool": tool_name, "args": tool_args, "requestId": request.id},
                )
            return self._build_response(request, {"status": "executing"})

        elif method == "ping":
            return self._build_response(request, {"pong": True})

        elif method == "resources/list":
            # P4-2: List workspace files as resources when orchestrator provides working_dir.
            resources = []
            try:
                from pathlib import Path as _Path
                import fnmatch

                _workdir = None
                if self._orchestrator is not None:
                    _workdir = getattr(self._orchestrator, "working_dir", None)
                if _workdir:
                    _base = _Path(_workdir)
                    _SKIP = {".git", "__pycache__", ".venv", "node_modules", ".agent-context"}
                    for p in sorted(_base.rglob("*")):
                        if any(part in _SKIP for part in p.parts):
                            continue
                        if p.is_file() and len(resources) < 200:
                            rel = str(p.relative_to(_base))
                            resources.append(
                                {"uri": f"file://{rel}", "name": rel, "mimeType": "text/plain"}
                            )
            except Exception as _re:
                logger.debug(f"MCPStdioServer: resources/list error: {_re}")
            result = {"resources": resources}
            return self._build_response(request, result)

        elif method == "resources/read":
            # P4-2: Read file contents by URI (file://<relative-path>).
            uri = params.get("uri", "")
            contents = []
            try:
                from pathlib import Path as _Path

                _workdir = None
                if self._orchestrator is not None:
                    _workdir = getattr(self._orchestrator, "working_dir", None)
                if _workdir and uri.startswith("file://"):
                    rel_path = uri[len("file://"):]
                    target = (_Path(_workdir) / rel_path).resolve()
                    base = _Path(_workdir).resolve()
                    # Security: reject path traversal outside working dir
                    if str(target).startswith(str(base)) and target.is_file():
                        text = target.read_text(encoding="utf-8", errors="replace")
                        contents = [{"uri": uri, "mimeType": "text/plain", "text": text}]
                    else:
                        return self._build_error_response(request, -32602, "Resource not found")
            except Exception as _re:
                return self._build_error_response(request, -32603, str(_re))
            result = {"contents": contents}
            return self._build_response(request, result)

        elif method == "prompts/list":
            # P4-2: Expose agent role prompts as named prompts.
            prompts = []
            try:
                from pathlib import Path as _Path

                _roles_dir = _Path(__file__).parents[3] / "config" / "agent-brain" / "roles"
                if _roles_dir.exists():
                    for f in sorted(_roles_dir.glob("*.md")):
                        prompts.append({"name": f.stem, "description": f"Role prompt: {f.stem}"})
            except Exception:
                pass
            result = {"prompts": prompts}
            return self._build_response(request, result)

        elif method == "prompts/get":
            # P4-2: Return the content of a named role prompt.
            name = params.get("name", "")
            messages = []
            try:
                from pathlib import Path as _Path

                _roles_dir = _Path(__file__).parents[3] / "config" / "agent-brain" / "roles"
                _prompt_file = _roles_dir / f"{name}.md"
                if _prompt_file.exists():
                    text = _prompt_file.read_text(encoding="utf-8")
                    messages = [{"role": "user", "content": {"type": "text", "text": text}}]
                else:
                    return self._build_error_response(request, -32602, f"Prompt '{name}' not found")
            except Exception as _pe:
                return self._build_error_response(request, -32603, str(_pe))
            result = {"messages": messages}
            return self._build_response(request, result)

        elif method == "sampling/create":
            # P4-2: Forward sampling request to orchestrator's LLM.
            messages_in = params.get("messages", [])
            max_tokens = params.get("maxTokens", 256)
            try:
                if self._orchestrator is not None and hasattr(self._orchestrator, "call_model"):
                    # Build minimal message list for call_model
                    _msgs = [{"role": m.get("role", "user"), "content": m.get("content", {}).get("text", "")} for m in messages_in]
                    import asyncio as _asyncio
                    resp = _asyncio.run(
                        self._orchestrator.call_model(_msgs, max_tokens=max_tokens)
                    )
                    if isinstance(resp, str):
                        text = resp
                    elif isinstance(resp, dict):
                        ch = (resp.get("choices") or [{}])[0].get("message", {})
                        text = ch.get("content", "") if isinstance(ch, dict) else str(ch)
                    else:
                        text = str(resp)
                    result = {"content": {"type": "text", "text": text}, "model": "coding-agent", "stopReason": "endTurn"}
                else:
                    result = {"content": {"type": "text", "text": ""}, "model": "coding-agent", "stopReason": "endTurn"}
            except Exception as _se:
                result = {"content": {"type": "text", "text": ""}, "model": "coding-agent", "stopReason": "error"}
            return self._build_response(request, result)

        elif method == "completion/complete":
            # P4-2: Return argument completions for known prompt/resource refs.
            ref = params.get("ref", {})
            _arg_name = params.get("argument", {}).get("name", "")
            _arg_val = params.get("argument", {}).get("value", "")
            completion_values = []
            try:
                if ref.get("type") == "ref/prompt":
                    # Suggest role prompt names matching the partial value
                    from pathlib import Path as _Path
                    _roles_dir = _Path(__file__).parents[3] / "config" / "agent-brain" / "roles"
                    if _roles_dir.exists():
                        completion_values = [
                            f.stem for f in _roles_dir.glob("*.md")
                            if f.stem.startswith(_arg_val)
                        ]
                elif ref.get("type") == "ref/resource":
                    # Suggest file URIs matching the partial value
                    _workdir = getattr(self._orchestrator, "working_dir", None) if self._orchestrator else None
                    if _workdir:
                        from pathlib import Path as _Path
                        _base = _Path(_workdir)
                        completion_values = [
                            f"file://{str(p.relative_to(_base))}"
                            for p in _base.rglob("*")
                            if p.is_file() and str(p.relative_to(_base)).startswith(_arg_val)
                        ][:20]
            except Exception:
                pass
            result = {"completion": {"values": completion_values, "hasMore": False}}
            return self._build_response(request, result)

        elif method == "logging/setLevel":
            # Set logging level
            level = params.get("level", "info")
            return self._build_response(request, {"status": "ok"})

        else:
            return self._build_error_response(
                request, -32601, f"Method not found: {method}"
            )

    def _handle_notification(self, notification: JsonRpcNotification) -> None:
        """Handle an incoming JSON-RPC notification."""
        method = notification.method
        params = notification.params

        # Forward notifications to EventBus
        eb = self._get_event_bus()
        if eb:
            eb.publish(f"mcp.{method}", params)

    def _event_bus_wildcard_handler(self, event_name: str) -> Callable:
        """Create a handler that forwards EventBus events to stdout as JSON-RPC."""

        def handler(payload: Any) -> None:
            try:
                # Convert EventBus event to MCP notification
                notification = {
                    "sessionUpdate": event_name,
                    "payload": payload,
                }
                # Extract standard sessionUpdate type if present
                if isinstance(payload, dict):
                    if "sessionUpdate" in payload:
                        notification = payload
                    elif "toolCallId" in payload:
                        # Already in ACP format
                        notification = payload

                output = self._build_notification("session/update", notification)
                print(output, flush=True)
            except Exception as e:
                logger.error(f"Failed to forward event {event_name}: {e}")

        return handler

    def _subscribe_to_event_bus(self) -> None:
        """Subscribe to EventBus wildcard for all events."""
        eb = self._get_event_bus()
        if not eb:
            return

        # Subscribe to wildcard to catch all events
        # GAP 3: Forward all EventBus events to stdout as JSON-RPC notifications
        topics = [
            "tool.execute.start",
            "tool.execute.finish",
            "tool.execute.error",
            "tool.invoked",
            "plan.progress",
            "plan.created",
            "plan.updated",
            "file.modified",
            "file.deleted",
            "file.read",
            "session.hydrated",
            "session.new",
            "session.files_changed",
            "model.routing",
            "model.response",
            "model.error",
            "token.budget.update",
            "preview.pending",
            "preview.accepted",
            "preview.rejected",
            "task.started",
            "task.completed",
            "task.failed",
            "task.cancelled",
            "ui.notification",
            "ui.status_update",
            "orchestrator.startup",
            "orchestrator.models.check.started",
            "orchestrator.models.check.completed",
            "orchestrator.models.check.failed",
        ]

        for topic in topics:
            eb.subscribe(topic, self._event_bus_wildcard_handler(topic))

        logger.info(f"MCPStdioServer: subscribed to {len(topics)} EventBus topics")

    def _read_stdin(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read and process stdin in a loop."""
        try:
            while self._running:
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip():
                    message = self._parse_json_rpc(line)
                    if message:
                        if isinstance(message, JsonRpcRequest):
                            response = self._handle_request(message)
                            if response:
                                print(response, flush=True)
                        elif isinstance(message, JsonRpcNotification):
                            self._handle_notification(message)
        except Exception as e:
            logger.error(f"Error reading stdin: {e}")
        finally:
            self._running = False

    async def run_async(self) -> None:
        """Run the MCP STDIO server asynchronously."""
        self._running = True
        self._subscribe_to_event_bus()

        logger.info("MCPStdioServer: starting (stdin/stdout mode)")

        # Run stdin reader in thread pool since it's blocking
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._read_stdin, loop)

    def run(self) -> None:
        """Run the MCP STDIO server synchronously."""
        self._running = True
        self._subscribe_to_event_bus()

        logger.info("MCPStdioServer: starting (stdin/stdout mode)")

        try:
            while self._running:
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip():
                    message = self._parse_json_rpc(line)
                    if message:
                        if isinstance(message, JsonRpcRequest):
                            response = self._handle_request(message)
                            if response:
                                print(response, flush=True)
                        elif isinstance(message, JsonRpcNotification):
                            self._handle_notification(message)
        except KeyboardInterrupt:
            logger.info("MCPStdioServer: shutting down")
        except Exception as e:
            logger.error(f"MCPStdioServer error: {e}")
        finally:
            self._running = False


def main():
    """Entry point for MCP STDIO server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting MCP STDIO Server...")

    server = MCPStdioServer()
    server.run()


if __name__ == "__main__":
    main()
