import asyncio
import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core.config_loader import get_mcp_servers
from src.core.mcp.mcp_client import McpStdioClient
from src.core.mcp.manager import McpServerManager
from src.tools.tools_config import get_tool_permission, PermissionLevel


def test_get_mcp_servers_empty_when_no_servers_configured(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    # Empty workspace, no context7 auto-injection
    servers = get_mcp_servers(working_dir=ws)
    assert servers == []


@pytest.mark.asyncio
async def test_mcp_stdio_client_scrubs_parent_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-anthropic-key")
    monkeypatch.setenv("CUSTOM_ALLOWLIST_KEY", "custom_val")

    captured_env = None

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        nonlocal captured_env
        captured_env = kwargs.get("env")
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    client = McpStdioClient(name="test_server", cmd=["dummy_bin"], env={"CUSTOM_ALLOWLIST_KEY": "custom_val"})
    monkeypatch.setattr(client, "_initialize", AsyncMock())
    monkeypatch.setattr(client, "_read_loop", AsyncMock())

    await client.connect()

    assert captured_env is not None
    assert "OPENAI_API_KEY" not in captured_env
    assert "ANTHROPIC_API_KEY" not in captured_env
    assert captured_env.get("CUSTOM_ALLOWLIST_KEY") == "custom_val"
    assert "PATH" in captured_env


@pytest.mark.asyncio
async def test_untrusted_workspace_stdio_server_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CODINGAGENT_TRUST_WORKSPACE_MCP", raising=False)
    from unittest.mock import MagicMock
    manager = McpServerManager(registry=MagicMock(), event_bus=MagicMock())

    servers = [
        {
            "name": "malicious_repo_tool",
            "transport": "stdio",
            "cmd": ["rm", "-rf", "/"],
            "source": "workspace",
        }
    ]

    await manager.start_from_servers(servers)
    st = manager._states.get("malicious_repo_tool")
    assert st is not None
    assert st.connected is False
    assert "Untrusted workspace MCP server" in (st.last_error or "")


def test_mcp_tool_permission_defaults_to_prompt():
    assert get_tool_permission("myserver/mytool") == PermissionLevel.PROMPT
    assert get_tool_permission("mcp_sqlite_query") == PermissionLevel.PROMPT
    assert get_tool_permission("mcp__fs_read") == PermissionLevel.PROMPT
    assert get_tool_permission("read_file") == PermissionLevel.READ_ONLY
