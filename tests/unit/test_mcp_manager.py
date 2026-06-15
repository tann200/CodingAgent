from __future__ import annotations

import pytest

from src.core.mcp.manager import McpServerManager


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event_name: str, payload: dict) -> None:
        self.events.append((event_name, payload))

    def publish_typed(self, event) -> None:
        self.events.append((event.__class__.__name__, event.to_dict()))


class _Registry:
    def register(self, *args, **kwargs):
        return None


class _FakeClient:
    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name = name
        self.cmd = cmd
        self.connected = False
        self.handlers = []
        self.register_calls = 0
        self._register_results = [1]

    def add_notification_handler(self, handler):
        self.handlers.append(handler)

    async def connect(self) -> None:
        if any("authfail" in part for part in self.cmd):
            raise RuntimeError("unauthorized token")
        self.connected = True

    async def register_tools(self, registry) -> int:
        self.register_calls += 1
        if self.register_calls <= len(self._register_results):
            return self._register_results[self.register_calls - 1]
        return self._register_results[-1]

    async def disconnect(self) -> None:
        self.connected = False

    async def emit(self, method: str, params: dict) -> None:
        for h in list(self.handlers):
            res = h(method, params)
            if hasattr(res, "__await__"):
                await res


@pytest.mark.asyncio
async def test_start_from_servers_connected_publishes_status(monkeypatch):
    bus = _Bus()
    reg = _Registry()
    manager = McpServerManager(registry=reg, event_bus=bus)

    created: list[_FakeClient] = []

    def _factory(name, cmd):
        c = _FakeClient(name, cmd)
        c._register_results = [2]
        created.append(c)
        return c

    monkeypatch.setattr("src.core.mcp.manager.McpStdioClient", _factory)

    await manager.start_from_servers(
        [{"name": "fs", "cmd": ["dummy-mcp"], "transport": "stdio"}],
        auto_register_default=True,
    )

    assert len(created) == 1
    assert created[0].register_calls == 1
    state = manager._states["fs"]
    assert state.connected is True
    assert state.tool_count == 2
    assert state.status == "connected"
    assert any(e[0] == "McpServerStatus" for e in bus.events)
    assert bus.events[-1][1]["count"] == 1


@pytest.mark.asyncio
async def test_start_from_servers_auth_error_sets_needs_auth(monkeypatch):
    bus = _Bus()
    reg = _Registry()
    manager = McpServerManager(registry=reg, event_bus=bus)

    monkeypatch.setattr("src.core.mcp.manager.McpStdioClient", _FakeClient)

    await manager.start_from_servers(
        [{"name": "private", "cmd": ["authfail-server"], "transport": "stdio"}],
        auto_register_default=True,
    )

    state = manager._states["private"]
    assert state.connected is False
    assert state.needs_auth is True
    assert state.status == "needs_auth"
    assert bus.events[-1][1].get("has_error", False) is True


@pytest.mark.asyncio
async def test_tools_list_changed_triggers_refresh(monkeypatch):
    bus = _Bus()
    reg = _Registry()
    manager = McpServerManager(registry=reg, event_bus=bus)

    created: list[_FakeClient] = []

    def _factory(name, cmd):
        c = _FakeClient(name, cmd)
        c._register_results = [1, 3]
        created.append(c)
        return c

    monkeypatch.setattr("src.core.mcp.manager.McpStdioClient", _factory)

    await manager.start_from_servers(
        [{"name": "fs", "cmd": ["dummy-mcp"], "transport": "stdio"}],
        auto_register_default=True,
    )
    assert manager._states["fs"].tool_count == 1

    await created[0].emit("tools/list_changed", {})

    assert created[0].register_calls == 2
    assert manager._states["fs"].tool_count == 3
    assert any(name == "McpToolsListChanged" for name, _ in bus.events)
