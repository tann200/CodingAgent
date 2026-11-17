"""Regression tests for TASK-18: dead artifact removal & MCPStdioServer hardening."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_dead_artifacts_removed():
    for rel in (
        "src/config/schema.json",
        "src/core/prompts/templates/plan_reminder.txt",
        "src/core/prompts/templates/build_switch.txt",
        "src/core/prompts/templates/max_steps.txt",
        "tui/tui_src/ui/components/cards.py",
        "tui/tui_src/config",
        "tui/tui_src/core",
        "tests/_ci_artifacts",
    ):
        assert not Path(rel).exists(), f"Dead artifact still exists: {rel}"


def test_mcp_stdio_server_roles_path_and_listener_cleanup(monkeypatch):
    from src.core.orchestration.mcp_stdio_server import MCPStdioServer, JsonRpcRequest

    srv = MCPStdioServer()
    req = JsonRpcRequest(id="1", method="prompts/list", params={})
    resp = json.loads(srv._handle_prompts_list(req))
    prompt_names = [p["name"] for p in resp["result"]["prompts"]]
    assert prompt_names, "Expected role prompts loaded from src/config/agent-brain/roles"

    req_comp = JsonRpcRequest(
        id="2",
        method="completion/complete",
        params={"ref": {"type": "ref/prompt"}, "argument": {"value": ""}},
    )
    comp_resp = json.loads(srv._handle_completion_complete(req_comp))
    assert comp_resp["result"]["completion"]["values"]

    fake_bus = MagicMock()
    srv._event_bus = fake_bus
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    srv.run()

    assert fake_bus.subscribe.call_count > 0
    assert fake_bus.unsubscribe.call_count == fake_bus.subscribe.call_count
    assert srv._subscribed_handlers == []
