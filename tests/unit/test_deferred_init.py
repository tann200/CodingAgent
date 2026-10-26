import asyncio
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.orchestration.deferred_init import (
    DeferredInitResult,
    is_trusted,
    run_deferred_init,
)
from src.core.orchestration.orchestrator import Orchestrator
from src.core.orchestration.tool_registry import ToolRegistry


def test_is_trusted_resolution(monkeypatch):
    """Verify priority order: orchestrator.trusted > env var > default False."""
    orch_untrusted = MagicMock()
    orch_untrusted.trusted = False
    orch_trusted = MagicMock()
    orch_trusted.trusted = True
    orch_none = MagicMock()
    orch_none.trusted = None

    # Default without env var
    monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
    assert is_trusted(None) is False
    assert is_trusted(orch_none) is False
    assert is_trusted(orch_untrusted) is False
    assert is_trusted(orch_trusted) is True

    # With env var set to 1
    monkeypatch.setenv("CODINGAGENT_TRUSTED", "1")
    assert is_trusted(None) is True
    assert is_trusted(orch_none) is True
    assert is_trusted(orch_trusted) is True
    # Explicit False overrides env var
    assert is_trusted(orch_untrusted) is False

    # With env var set to yes / true
    monkeypatch.setenv("CODINGAGENT_TRUSTED", "true")
    assert is_trusted(orch_none) is True
    monkeypatch.setenv("CODINGAGENT_TRUSTED", "yes")
    assert is_trusted(orch_none) is True
    monkeypatch.setenv("CODINGAGENT_TRUSTED", "0")
    assert is_trusted(orch_none) is False


def test_untrusted_workspace_skips_mcp_and_plugins(tmp_path):
    """When workspace is untrusted, run_deferred_init must not start MCP or load plugins."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "sample_plugin.py"
    plugin_file.write_text("PLUGIN_INITIALIZED = True\n")

    mcp_mock = MagicMock()
    mcp_mock.start = MagicMock()

    class FakeOrch:
        def __init__(self):
            self.trusted = False
            self.plugin_dir = plugin_dir
            self.mcp_manager = mcp_mock
            self._tool_hook_runner = MagicMock()

    orch = FakeOrch()
    result = asyncio.run(run_deferred_init(orch, trusted=False))

    assert isinstance(result, DeferredInitResult)
    assert result.mcp_started is False
    assert result.hooks_enabled is False
    assert result.plugins_loaded == 0
    mcp_mock.start.assert_not_called()
    assert "codingagent.plugins.sample_plugin" not in sys.modules


def test_trusted_workspace_loads_plugins_and_registers_tools(tmp_path):
    """When trusted, plugins in plugin_dir are executed and their tools registered."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "my_custom_plugin.py"
    plugin_file.write_text(
        "from src.tools._tool import tool\n\n"
        "@tool(name='custom_plugin_tool', description='A custom plugin tool')\n"
        "def custom_plugin_tool(x: int) -> int:\n"
        "    return x * 2\n"
    )

    registry = ToolRegistry()
    mcp_mock = MagicMock()

    async def fake_start():
        return None

    mcp_mock.start = fake_start

    class FakeOrch:
        def __init__(self):
            self.trusted = True
            self.working_dir = tmp_path
            self.plugin_dir = plugin_dir
            self.mcp_manager = mcp_mock
            self._tool_hook_runner = MagicMock()
            self.tool_registry = registry

    orch = FakeOrch()
    result = asyncio.run(run_deferred_init(orch, trusted=True))

    assert result.mcp_started is True
    assert result.hooks_enabled is True
    assert result.plugins_loaded == 1
    assert "codingagent.plugins.my_custom_plugin" in sys.modules
    assert "custom_plugin_tool" in registry


def test_default_candidate_plugin_dir_discovery(tmp_path):
    """When plugin_dir is None, .codingagent/plugins inside working_dir is discovered."""
    plugins_dir = tmp_path / ".codingagent" / "plugins"
    plugins_dir.mkdir(parents=True)
    plugin_file = plugins_dir / "auto_found_plugin.py"
    plugin_file.write_text("AUTO_FOUND = 42\n")

    class FakeOrch:
        def __init__(self):
            self.trusted = True
            self.working_dir = tmp_path
            self.plugin_dir = None
            self.tool_registry = ToolRegistry()

    orch = FakeOrch()
    result = asyncio.run(run_deferred_init(orch, trusted=True))

    assert result.plugins_loaded == 1
    assert "codingagent.plugins.auto_found_plugin" in sys.modules


def test_world_writable_plugin_dir_rejected(tmp_path):
    """Refuse to load plugins from world-writable directories."""
    plugin_dir = tmp_path / "insecure_plugins"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "evil.py"
    plugin_file.write_text("PWNED = True\n")

    # Make world-writable
    try:
        os.chmod(plugin_dir, 0o777)
    except OSError:
        pytest.skip("Cannot change permissions on this filesystem")

    # Check if S_IWOTH is actually set
    if not (os.stat(plugin_dir).st_mode & stat.S_IWOTH):
        pytest.skip("Filesystem does not support S_IWOTH")

    class FakeOrch:
        def __init__(self):
            self.trusted = True
            self.plugin_dir = plugin_dir
            self.tool_registry = ToolRegistry()

    orch = FakeOrch()
    result = asyncio.run(run_deferred_init(orch, trusted=True))

    assert result.plugins_loaded == 0
    assert any("world-writable" in err for err in result.errors)
    assert "codingagent.plugins.evil" not in sys.modules


def test_orchestrator_run_deferred_init_sync_and_async(tmp_path):
    """Test Orchestrator sync and async methods for deferred init."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "orch_sync_test.py").write_text("SYNC_TEST = 1\n")

    orch = Orchestrator(
        working_dir=tmp_path,
        trusted=True,
        plugin_dir=plugins_dir,
        dry_run=True,
    )

    assert orch.trusted is True
    assert orch._deferred_init_done is False
    assert orch._deferred_init_result is None

    # Run sync
    res1 = orch.run_deferred_init_sync()
    assert orch._deferred_init_done is True
    assert res1.plugins_loaded == 1

    # Idempotent second call
    res2 = orch.run_deferred_init_sync()
    assert res2 is res1


@pytest.mark.asyncio
async def test_orchestrator_run_deferred_init_async(tmp_path):
    """Test Orchestrator async method for deferred init."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "orch_async_test.py").write_text("ASYNC_TEST = 1\n")

    orch = Orchestrator(
        working_dir=tmp_path,
        trusted=True,
        plugin_dir=plugins_dir,
        dry_run=True,
    )

    res = await orch.run_deferred_init_async()
    assert orch._deferred_init_done is True
    assert res.plugins_loaded == 1


def test_mcp_manager_trust_integration(tmp_path, monkeypatch):
    """Test McpServerManager respects workspace trust flag."""
    from src.core.mcp.manager import McpServerManager

    registry = ToolRegistry()
    mcp_mgr = McpServerManager(
        registry=registry,
        event_bus=None,
        working_dir=tmp_path,
        trusted=False,
    )

    # Workspace stdio server
    servers = [
        {
            "name": "untrusted_server",
            "transport": "stdio",
            "cmd": ["echo", "hi"],
            "source": "workspace",
            "trusted": False,
        }
    ]

    monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
    monkeypatch.delenv("CODINGAGENT_TRUST_WORKSPACE_MCP", raising=False)

    asyncio.run(mcp_mgr.start_from_servers(servers))
    st = mcp_mgr._states.get("untrusted_server")
    assert st is not None
    assert st.connected is False
    assert "Untrusted workspace MCP server" in st.last_error

    # Now with trusted=True
    mcp_mgr_trusted = McpServerManager(
        registry=registry,
        event_bus=None,
        working_dir=tmp_path,
        trusted=True,
    )
    # The server will try to connect and run echo, but the trust gate should pass
    # (echo is not a real MCP JSON-RPC server so connect fails with handshake error,
    # but NOT with "Untrusted workspace MCP server")
    asyncio.run(mcp_mgr_trusted.start_from_servers(servers))
    st2 = mcp_mgr_trusted._states.get("untrusted_server")
    assert st2 is not None
    assert "Untrusted workspace MCP server" not in st2.last_error


def test_cli_trusted_flags():
    """Verify that --trusted, --untrusted, and --plugin-dir are parsed by main.py."""
    from src.main import _parse_args

    args_trusted = _parse_args(["--trusted", "--task", "test"])
    assert args_trusted.trusted is True

    args_untrusted = _parse_args(["--untrusted", "--task", "test"])
    assert args_trusted.trusted is True
    assert args_untrusted.trusted is False

    args_custom = _parse_args(["--plugin-dir", "/tmp/plugins", "--task", "test"])
    assert args_custom.plugin_dir == "/tmp/plugins"
    assert args_custom.trusted is None


def test_inference_loop_triggers_deferred_init(tmp_path):
    """inference_loop.run_agent_once_impl must trigger run_deferred_init_sync if not done."""
    from src.core.orchestration.inference_loop import run_agent_once_impl

    orch = Orchestrator(
        working_dir=tmp_path,
        dry_run=True,
        trusted=False,
    )
    assert orch._deferred_init_done is False

    cancel_event = MagicMock()
    cancel_event.is_set.return_value = True  # Cancel immediately after setup

    res = run_agent_once_impl(
        orch,
        system_prompt_name="operational",
        messages=[{"role": "user", "content": "hi"}],
        tools={},
        cancel_event=cancel_event,
    )
    assert orch._deferred_init_done is True
    assert orch._deferred_init_result is not None
    assert res.get("ok") is False
    assert res.get("error") == "canceled_before_start"
