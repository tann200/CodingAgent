"""Outbound MCP server manager.

Coordinates multiple configured MCP servers, registers their tools into the
agent tool registry, and publishes aggregated health/status events.

Current transport support:
- ``stdio`` (implemented via :class:`McpStdioClient`)

Future transports (Gap 1):
- ``sse``
- ``http`` / ``streamable_http``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config_loader import get_mcp_config, get_mcp_servers
from src.core.mcp.mcp_client import McpStdioClient

logger = logging.getLogger(__name__)


@dataclass
class McpServerState:
    """Runtime status for one outbound MCP server."""

    name: str
    transport: str = "stdio"
    connected: bool = False
    needs_auth: bool = False
    tool_count: int = 0
    last_error: str = ""

    @property
    def status(self) -> str:
        if self.connected:
            return "connected"
        if self.needs_auth:
            return "needs_auth"
        if self.last_error:
            return "failed"
        return "disconnected"


class McpServerManager:
    """Manage configured outbound MCP servers.

    The manager is safe to call repeatedly: ``start()`` is idempotent when
    already started and ``stop()`` tolerates partial failures.
    """

    def __init__(
        self,
        *,
        registry: Any,
        event_bus: Any,
        working_dir: Optional[Path] = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._working_dir = working_dir
        self._clients: Dict[str, McpStdioClient] = {}
        self._states: Dict[str, McpServerState] = {}
        self._started: bool = False

    def _normalize_server_cmd(self, cfg: Dict[str, Any]) -> List[str]:
        cmd = cfg.get("cmd")
        if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
            return list(cmd)

        command = cfg.get("command")
        args = cfg.get("args")
        if isinstance(command, str) and command.strip():
            out = [command.strip()]
            if isinstance(args, list):
                out.extend(str(a) for a in args)
            return out
        return []

    @staticmethod
    def _is_auth_error(msg: str) -> bool:
        m = msg.lower()
        return any(
            token in m
            for token in (
                "unauthorized",
                "access denied",
                "forbidden",
                "oauth",
                "auth",
                "token",
            )
        )

    def _publish_status(self) -> None:
        connected_names = [s.name for s in self._states.values() if s.connected]
        has_error = any(
            (not s.connected) and (s.last_error or s.needs_auth)
            for s in self._states.values()
        )
        payload = {
            "running": bool(connected_names),
            "count": len(connected_names),
            "server_names": connected_names,
            "has_error": has_error,
            "servers": {
                name: {
                    "transport": st.transport,
                    "status": st.status,
                    "connected": st.connected,
                    "needs_auth": st.needs_auth,
                    "tool_count": st.tool_count,
                    "error": st.last_error,
                }
                for name, st in self._states.items()
            },
        }
        try:
            self._event_bus.publish("mcp.server.status", payload)
        except Exception:
            pass

    async def _on_server_notification(
        self, server_name: str, method: str, params: Dict[str, Any]
    ) -> None:
        if method in ("tools/list_changed", "notifications/tools/list_changed"):
            try:
                await self.refresh_tools(server_name)
            except Exception as exc:
                logger.debug(
                    "mcp manager: refresh after tools/list_changed failed for %s: %s",
                    server_name,
                    exc,
                )
            try:
                self._event_bus.publish(
                    "mcp.tools.list_changed",
                    {"server": server_name, "params": params},
                )
            except Exception:
                pass

    async def refresh_tools(self, server_name: str) -> int:
        """Re-list and register tools from one connected server."""
        client = self._clients.get(server_name)
        if client is None:
            return 0
        count = await client.register_tools(self._registry)
        st = self._states.get(server_name)
        if st is not None:
            st.tool_count = count
            st.connected = True
            st.last_error = ""
            st.needs_auth = False
        self._publish_status()
        return count

    async def start_from_servers(
        self, servers: List[Dict[str, Any]], *, auto_register_default: bool = True
    ) -> None:
        """Start and connect all configured outbound MCP servers."""
        if self._started:
            return

        self._states.clear()
        self._clients.clear()

        for cfg in servers:
            name = str(cfg.get("name") or "").strip()
            if not name:
                continue
            transport = str(cfg.get("transport") or "stdio").strip().lower()
            st = McpServerState(name=name, transport=transport)
            self._states[name] = st

            if transport != "stdio":
                st.last_error = f"Unsupported MCP transport: {transport}"
                self._publish_status()
                continue

            cmd = self._normalize_server_cmd(cfg)
            if not cmd:
                st.last_error = "Missing MCP server command (cmd/command)"
                self._publish_status()
                continue

            client = McpStdioClient(name=name, cmd=cmd)
            client.add_notification_handler(
                lambda method, params, _name=name: self._on_server_notification(
                    _name, method, params
                )
            )

            try:
                await client.connect()
                st.connected = True
                self._clients[name] = client

                should_register = cfg.get("auto_register_tools")
                if should_register is None:
                    should_register = auto_register_default
                if should_register:
                    st.tool_count = await client.register_tools(self._registry)

            except Exception as exc:  # noqa: BLE001
                st.connected = False
                st.last_error = str(exc)
                st.needs_auth = self._is_auth_error(st.last_error)
                logger.warning("mcp manager: server %s failed to start: %s", name, exc)

            self._publish_status()

        self._started = True

    async def start(self) -> None:
        """Load config and start all configured MCP servers."""
        cfg = get_mcp_config(self._working_dir)
        auto_register_default = bool(cfg.get("auto_register_tools", True))
        servers = get_mcp_servers(self._working_dir)
        await self.start_from_servers(
            servers,
            auto_register_default=auto_register_default,
        )

    async def stop(self) -> None:
        """Disconnect all connected MCP servers."""
        for name, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:  # pragma: no cover
                logger.debug("mcp manager: disconnect failed for %s: %s", name, exc)
            st = self._states.get(name)
            if st is not None:
                st.connected = False
        self._clients.clear()
        self._started = False
        self._publish_status()
