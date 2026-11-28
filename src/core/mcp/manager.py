"""Outbound MCP server manager.

Coordinates multiple configured MCP servers, registers their tools into the
agent tool registry, and publishes aggregated health/status events.

Transport support:
- ``stdio`` (local subprocess)
- ``http`` (HTTP Streamable)
- ``sse`` (Server-Sent Events)
- ``ws`` / ``websocket`` (WebSocket)
"""

from __future__ import annotations


from src.core.messaging.event_types import McpServerStatus, McpToolsListChanged
import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config_loader import get_mcp_config, get_mcp_servers
from src.core.mcp.mcp_client import McpStdioClient
from src.core.mcp.mcp_http_client import McpHttpClient
from src.core.mcp.mcp_sse_client import McpSseClient
from src.core.mcp.mcp_ws_client import McpWsClient
import os

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


def _wrap_mcp_output(value: Any, tool_name: str) -> Any:
    """Wrap untrusted string payloads returned by MCP servers in a nonce-fenced envelope."""
    from src.core.security.untrusted_envelopes import PROCESS_NONCE, wrap_untrusted

    marker = f'<mcp_result nonce="{PROCESS_NONCE}"'
    if isinstance(value, str):
        if value.startswith(marker):
            return value
        return wrap_untrusted("mcp_result", value, attrs={"tool": tool_name})
    if isinstance(value, dict):
        wrapped = dict(value)
        for key in ("content", "output", "text", "result"):
            val = wrapped.get(key)
            if isinstance(val, str) and val and not val.startswith(marker):
                wrapped[key] = wrap_untrusted("mcp_result", val, attrs={"tool": tool_name})
        return wrapped
    return value


class _LoopTaggingRegistryProxy:
    """Proxy around a ToolRegistry that stamps ``_mcp_loop`` and wraps MCP outputs."""

    def __init__(self, target: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        self._target = target
        self._loop = loop

    def register(self, name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        tagged = fn
        if callable(fn):
            _orig = fn
            if inspect.iscoroutinefunction(fn):
                async def _wrapped_async(*a: Any, **kw: Any) -> Any:
                    return _wrap_mcp_output(await _orig(*a, **kw), name)

                if self._loop is not None:
                    setattr(_wrapped_async, "_mcp_loop", self._loop)
                tagged = _wrapped_async
            else:
                def _wrapped_sync(*a: Any, **kw: Any) -> Any:
                    return _wrap_mcp_output(_orig(*a, **kw), name)

                if self._loop is not None:
                    setattr(_wrapped_sync, "_mcp_loop", self._loop)
                tagged = _wrapped_sync
        return self._target.register(name, tagged, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target, item)


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
        trusted: Optional[bool] = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._working_dir = working_dir
        self._trusted = trusted
        self._clients: Dict[str, Any] = {}
        self._states: Dict[str, McpServerState] = {}
        self._started: bool = False
        self._lock = threading.RLock()
        # TASK-21: Persistent daemon event loop for sync callers (e.g. run_deferred_init_sync)
        self._bg_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bg_thread: Optional[threading.Thread] = None
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None

    def _ensure_bg_loop(self) -> asyncio.AbstractEventLoop:
        """Return (creating lazily) a long-lived daemon event loop for MCP transports."""
        with self._lock:
            if (
                self._bg_loop is not None
                and not self._bg_loop.is_closed()
                and self._bg_thread is not None
                and self._bg_thread.is_alive()
            ):
                return self._bg_loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    try:
                        loop.close()
                    except Exception:
                        pass

            thread = threading.Thread(
                target=_run_loop, name="mcp-event-loop", daemon=True
            )
            thread.start()
            ready.wait(timeout=5.0)
            self._bg_loop = loop
            self._bg_thread = thread
            return loop

    def run_coro_sync(self, coro: Any, timeout: Optional[float] = None) -> Any:
        """Execute *coro* on the manager's persistent daemon event loop."""
        loop = self._ensure_bg_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    def start_sync(self, timeout: Optional[float] = None) -> None:
        """Start configured MCP servers synchronously on the persistent daemon loop."""
        self.run_coro_sync(self.start(), timeout=timeout)

    def _wrap_registry_for_loop(self, registry: Any) -> Any:
        running_loop: Optional[asyncio.AbstractEventLoop]
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = self._owner_loop
        if running_loop is not None:
            self._owner_loop = running_loop
            return _LoopTaggingRegistryProxy(registry, running_loop)
        return registry

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
        with self._lock:
            connected_names = [s.name for s in self._states.values() if s.connected]
            has_error = any(
                (not s.connected) and (s.last_error or s.needs_auth)
                for s in self._states.values()
            )
            payload: Dict[str, Any] = {
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
            self._event_bus.publish_typed(
                McpServerStatus(
                    running=bool(payload["running"]),
                    count=payload["count"],
                    server_names=list(payload["server_names"]),
                    has_error=bool(payload["has_error"]),
                    servers=payload["servers"],
                )
            )
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
                self._event_bus.publish_typed(McpToolsListChanged(server=server_name, params=params))
            except Exception:
                pass

    async def refresh_tools(self, server_name: str) -> int:
        """Re-list and register tools from one connected server."""
        with self._lock:
            client = self._clients.get(server_name)
            if client is None:
                return 0
        count = await client.register_tools(self._wrap_registry_for_loop(self._registry))
        with self._lock:
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
        with self._lock:
            if self._started:
                return
            # Snapshot existing clients and clear state BEFORE awaiting disconnect (never hold RLock across await).
            existing_clients = list(self._clients.items())
            self._states.clear()
            self._clients.clear()

        for _name, _client in existing_clients:
            try:
                await _client.disconnect()
            except Exception:
                pass

        for cfg in servers:
            name = str(cfg.get("name") or "").strip()
            if not name:
                continue
            transport = str(cfg.get("transport") or "stdio").strip().lower()

            supported_transports = {"stdio", "http", "sse", "ws", "websocket"}
            if transport not in supported_transports:
                with self._lock:
                    st = McpServerState(name=name, transport=transport)
                    self._states[name] = st
                    st.last_error = f"Unsupported MCP transport: {transport}"
                self._publish_status()
                continue

            # SPEC-03: Workspace-defined MCP servers across ALL transports require explicit trust
            source = cfg.get("source")
            if self._trusted is False:
                is_trusted_workspace = False
            else:
                is_trusted_workspace = (
                    self._trusted is True
                    or os.environ.get("CODINGAGENT_TRUSTED", "").strip().lower() in ("1", "true", "yes")
                    or os.environ.get("CODINGAGENT_TRUST_WORKSPACE_MCP") == "1"
                )
            if source == "workspace":
                # REM-04 (SEC-04): Never allow untrusted workspace entries to
                # self-certify via cfg["trusted"]=True — require explicit trust.
                if not is_trusted_workspace:
                    logger.warning(
                        "mcp manager: refusing to auto-start untrusted workspace %s MCP server %r. "
                        "Set CODINGAGENT_TRUST_WORKSPACE_MCP=1 or mark workspace as trusted to allow.",
                        transport,
                        name,
                    )
                    with self._lock:
                        st = McpServerState(name=name, transport=transport)
                        self._states[name] = st
                        st.last_error = "Untrusted workspace MCP server (requires approval)"
                    self._publish_status()
                    continue

            url = cfg.get("url") or cfg.get("endpoint")
            cmd = self._normalize_server_cmd(cfg)
            cfg_env = cfg.get("env") if isinstance(cfg.get("env"), dict) else None
            if cfg_env is None:
                cfg_env = {}

            # REM-04 (SEC-10): Only auto-inject host CONTEXT7_API_KEY for the exact
            # official mcp.context7.com host (or a user-global context7 stdio config),
            # never substring-matching arbitrary workspace names/URLs.
            from urllib.parse import urlparse as _urlparse

            _url_host = (_urlparse(str(url or "")).hostname or "").lower()
            _is_official_context7 = _url_host in ("mcp.context7.com", "context7.com") or (
                source == "global" and transport == "stdio" and "context7" in name.lower()
            )
            if (
                _is_official_context7
                and "CONTEXT7_API_KEY" not in cfg_env
                and os.environ.get("CONTEXT7_API_KEY")
            ):
                cfg_env["CONTEXT7_API_KEY"] = os.environ.get("CONTEXT7_API_KEY")

            # Ensure we have a server state object for reporting/status updates
            with self._lock:
                st = McpServerState(name=name, transport=transport)
                self._states[name] = st

            client: Any = None
            if transport in ("http", "sse", "ws", "websocket") and url:
                ws_url = str(url)
                # Build headers from cfg or environment API key
                headers: Dict[str, str] = {}
                # Allow cfg to specify headers directly
                if isinstance(cfg.get("headers"), dict):
                    headers.update(
                        {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
                    )
                # If an API key is available in cfg_env, prefer it for Authorization
                api_key = cfg_env.get("CONTEXT7_API_KEY")
                if api_key:
                    headers.setdefault("Authorization", f"Bearer {api_key}")

                if transport == "sse":
                    client = McpSseClient(name=name, url=ws_url, headers=headers)
                elif transport in ("ws", "websocket"):
                    client = McpWsClient(name=name, url=ws_url, headers=headers)
                else:
                    client = McpHttpClient(name=name, url=ws_url, headers=headers)
            elif transport == "stdio" and cmd:
                # Some tests monkeypatch McpStdioClient with factories that do
                # not accept the new "env" keyword. Be backwards-compatible by
                # trying the new signature first and falling back to the
                # original (name, cmd) form on TypeError.
                try:
                    client = McpStdioClient(name=name, cmd=cmd, env=cfg_env)
                except TypeError:
                    # Fall back to older factory signature
                    try:
                        client = McpStdioClient(name=name, cmd=cmd)
                    except Exception:  # pragma: no cover - defensive
                        raise

                # Safely attach notification handler if supported
                try:
                    def _notify(
                        method: str, params: Dict[str, Any], _name: str = name
                    ) -> Any:
                        return self._on_server_notification(_name, method, params)

                    client.add_notification_handler(_notify)
                except Exception:
                    # Some client implementations may not support notifications;
                    # ignore and continue.
                    pass
            else:
                with self._lock:
                    st = McpServerState(name=name, transport=transport)
                    self._states[name] = st
                    if transport in ("http", "sse", "ws", "websocket"):
                        st.last_error = "Missing MCP server URL (url/endpoint)"
                    else:
                        st.last_error = "Missing MCP server command (cmd/command)"
                self._publish_status()
                continue

            try:
                await client.connect()
                with self._lock:
                    st.connected = True
                    self._clients[name] = client

                should_register = cfg.get("auto_register_tools")
                if should_register is None:
                    should_register = auto_register_default
                if should_register:
                    count = await client.register_tools(
                        self._wrap_registry_for_loop(self._registry)
                    )
                    with self._lock:
                        st.tool_count = count

            except Exception as exc:  # noqa: BLE001
                with self._lock:
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
        with self._lock:
            clients_snapshot = list(self._clients.items())
            names = [n for n, _ in clients_snapshot]
        for name, client in clients_snapshot:
            if client is None:
                continue
            try:
                await client.disconnect()
            except Exception as exc:  # pragma: no cover
                logger.debug("mcp manager: disconnect failed for %s: %s", name, exc)
        with self._lock:
            for name in names:
                st = self._states.get(name)
                if st is not None:
                    st.connected = False
            self._clients.clear()
            self._started = False
        self._publish_status()

    def stop_sync(self) -> None:
        """Synchronously disconnect MCP clients, kill remaining subprocesses, and close the daemon loop."""
        with self._lock:
            bg_loop = self._bg_loop
            bg_thread = self._bg_thread

        # Gracefully disconnect all async clients (HTTP/SSE/WS/stdio) on the
        # persistent daemon loop before tearing it down.
        if (
            bg_loop is not None
            and not bg_loop.is_closed()
            and bg_loop.is_running()
            and threading.current_thread() is not bg_thread
        ):
            try:
                fut = asyncio.run_coroutine_threadsafe(self.stop(), bg_loop)
                fut.result(timeout=5.0)
            except Exception as exc:
                logger.debug("mcp manager: stop_sync async disconnect failed: %s", exc)

        # Fallback: terminate any remaining stdio subprocesses directly.
        with self._lock:
            for name, client in list(self._clients.items()):
                proc = getattr(client, "_process", None)
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                st = self._states.get(name)
                if st is not None:
                    st.connected = False
            self._clients.clear()
            self._started = False
            self._bg_loop = None
            self._bg_thread = None
            self._owner_loop = None

        if bg_loop is not None and not bg_loop.is_closed():
            try:
                bg_loop.call_soon_threadsafe(bg_loop.stop)
            except Exception:
                pass
        if (
            bg_thread is not None
            and bg_thread.is_alive()
            and threading.current_thread() is not bg_thread
        ):
            bg_thread.join(timeout=3.0)

    async def re_register_tools(self, registry: Any) -> None:
        """Re-register all connected MCP clients' tools into a new registry."""
        with self._lock:
            self._registry = registry
            clients = list(self._clients.values())
        wrapped = self._wrap_registry_for_loop(registry)
        for client in clients:
            if hasattr(client, "register_tools"):
                try:
                    await client.register_tools(wrapped)
                except Exception as exc:
                    logger.debug("Failed to re-register MCP tools: %s", exc)
