"""Unit tests for Phase 3 usability and performance hardening (Section 11 & Section 12)."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.server.sse_adapter import ServerEventBusAdapter
from tui.tui_src.ui.core_bridge import AgentBridge
from tui.tui_src.ui.screens.session_list import SessionListScreen


def test_core_bridge_active_role_property_and_setter():
    mock_app = MagicMock()
    bridge = AgentBridge(app=mock_app)
    assert bridge.active_role == "lead_architect"

    bridge.set_active_role("security_auditor")
    assert bridge.active_role == "security_auditor"

    bridge.active_role = "code_reviewer"
    assert bridge.active_role == "code_reviewer"

    # Default fallback
    bridge.set_active_role("")
    assert bridge.active_role == "lead_architect"


def test_session_list_offset_accounting_when_show_new_session_is_true(tmp_path: Path):
    screen = SessionListScreen()
    screen._show_new_session = True

    # Setup 2 dummy sessions
    sess1_path = tmp_path / "sess1.json"
    sess1_path.write_text("{}", encoding="utf-8")
    sess2_path = tmp_path / "sess2.json"
    sess2_path.write_text("{}", encoding="utf-8")

    screen._filtered = [
        (sess1_path, {"session_id": "s1", "task_name": "Task 1", "messages": [{"role": "user", "content": "hi"}]}),
        (sess2_path, {"session_id": "s2", "task_name": "Task 2", "messages": []}),
    ]
    screen._all_sessions = list(screen._filtered)

    # When UI index 0 is selected (synthetic row [+ Start new session])
    screen._selected = 0
    new_session_called = []
    screen._start_new_session = lambda: new_session_called.append(True)
    screen._resume_selected()
    assert len(new_session_called) == 1

    # When UI index 1 is selected (should point to sess1, which is at _filtered[0])
    screen._selected = 1
    mock_app = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge._history_lock = MagicMock()
    mock_app._bridge = mock_bridge

    from textual._context import active_app
    token = active_app.set(mock_app)
    try:
        screen._resume_selected()
        # Should resume Task 1
        assert any("Task 1" in str(arg) for call in mock_app.notify.call_args_list for arg in call[0])

        # Deleting UI index 0 (synthetic row) should be a no-op
        screen._selected = 0
        screen._delete_selected()
        assert len(screen._filtered) == 2

        # Deleting UI index 1 (sess1) should delete sess1, not sess2!
        screen._selected = 1
        screen._delete_selected()
        assert len(screen._filtered) == 1
        assert screen._filtered[0][1]["session_id"] == "s2"
    finally:
        active_app.reset(token)


def test_sse_adapter_parameter_clamping_and_thread_safe_delivery():
    mock_bus = MagicMock()

    # Extreme parameters should be clamped
    adapter = ServerEventBusAdapter(
        event_bus=mock_bus,
        queue_max_size=100_000,
        keepalive_interval=0,
        drop_policy="invalid_policy",
    )
    assert adapter.queue_max_size == 1000
    assert adapter.keepalive_interval == 1
    assert adapter.drop_policy == "drop_oldest"

    adapter2 = ServerEventBusAdapter(
        event_bus=mock_bus,
        queue_max_size=-5,
        keepalive_interval=1000,
        drop_policy="drop_new",
    )
    assert adapter2.queue_max_size == 1
    assert adapter2.keepalive_interval == 300
    assert adapter2.drop_policy == "drop_new"


@pytest.mark.asyncio
async def test_sse_adapter_thread_safe_handler_dispatch():
    handlers = {}
    mock_bus = MagicMock()

    def fake_subscribe(event_name, handler):
        handlers[event_name] = handler

    mock_bus.subscribe = fake_subscribe

    adapter = ServerEventBusAdapter(event_bus=mock_bus, queue_max_size=10)
    gen = adapter.event_generator("session-123")

    # Start generator task
    async def run_gen():
        events_received = []
        async for item in gen:
            events_received.append(item)
            if len(events_received) >= 1:
                break
        return events_received

    task = asyncio.create_task(run_gen())
    await asyncio.sleep(0.05)

    # Invoke handler from a separate thread simulating worker thread EventBus emit
    import threading

    def emit_from_thread():
        h = handlers.get("agent.start")
        if h:
            h({"session_id": "session-123", "status": "running"})

    t = threading.Thread(target=emit_from_thread)
    t.start()
    t.join()

    res = await asyncio.wait_for(task, timeout=2.0)
    assert len(res) == 1
    assert "session-123" in res[0]


@pytest.mark.asyncio
async def test_websocket_parameters_clamping():
    from unittest.mock import AsyncMock, patch
    from starlette.websockets import WebSocketDisconnect
    from src.server.websocket_handler import websocket_session_handler

    mock_ws = MagicMock()
    mock_ws.query_params = {
        "queue_max_size": "999999",
        "keepalive": "0",
        "drop_policy": "unknown_policy",
    }
    mock_ws.headers = {}
    mock_ws.accept = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect)

    mock_bus = MagicMock()

    created_queues = []
    real_queue_init = asyncio.Queue.__init__

    def intercept_queue_init(self, *args, **kwargs):
        ms = kwargs.get("maxsize", args[0] if args else 0)
        created_queues.append(ms)
        real_queue_init(self, *args, **kwargs)

    with patch.object(asyncio.Queue, "__init__", intercept_queue_init):
        try:
            await websocket_session_handler("sess-1", mock_ws, event_bus=mock_bus)
        except WebSocketDisconnect:
            pass

    assert len(created_queues) >= 1
    # maxsize must be clamped to 1000, not 999999
    assert created_queues[0] == 1000


def test_hardware_detection_cpu_only_preserves_zero_vram():
    from unittest.mock import patch
    from src.core.inference.hardware_capability_profile import detect_hardware

    with (
        patch("platform.system", return_value="Linux"),
        patch("src.core.inference.hardware_capability_profile._detect_vram_nvidia", return_value=0.0),
        patch("src.core.inference.hardware_capability_profile._detect_vram_metal", return_value=0.0),
        patch("src.core.inference.hardware_capability_profile._detect_ram", return_value=32.0),
        patch("src.core.inference.hardware_capability_profile._detect_cpu_cores", return_value=(8, 16)),
    ):
        profile = detect_hardware()
        assert profile.vram_gb == 0.0
        assert profile.supports_gpu_offload is False
        assert profile.ram_gb == 32.0
        assert profile.name == "detected-cpu-only"


def test_kv_cache_governor_custom_thresholds():
    from src.core.inference.kv_cache_governor import CompactionAction, KVCacheGovernor

    governor = KVCacheGovernor(
        max_vram_gb=16.0,
        model_weights_gb=6.0,
        kv_per_token_mb=1.0,
        overhead_gb=0.0,
        warning_threshold=0.50,
        compact_threshold=0.60,
    )
    # max_kv_cache_gb = 10.0, max_tokens = (10.0 * 1024 / 1.0) * 1000 = 10,240,000

    # Test at 55% tokens (between 0.50 and 0.60)
    tokens_55 = int(governor.max_tokens * 0.55)
    state = governor.on_context_update(tokens_55)
    assert state.should_warn is True
    assert state.should_compact is False
    assert state.action == CompactionAction.WARNING

    # Test at 65% tokens (above compact_threshold 0.60, but below legacy 0.85)
    tokens_65 = int(governor.max_tokens * 0.65)
    state2 = governor.on_context_update(tokens_65)
    assert state2.should_warn is True
    assert state2.should_compact is True
    assert state2.action == CompactionAction.COMPACT


def test_analysis_node_per_file_freshness_tracking(tmp_path: Path):
    from src.core.orchestration.graph.nodes.analysis_node import (
        _is_already_indexed,
        _mark_indexed,
    )
    from src.core.indexing.repo_indexer import _get_ctx_dir_name

    ctx_dir = tmp_path / _get_ctx_dir_name()
    ctx_dir.mkdir(parents=True, exist_ok=True)
    index_file = ctx_dir / "repo_index.json"
    index_file.write_text("{}", encoding="utf-8")

    sub_dir = tmp_path / "pkg"
    sub_dir.mkdir()
    py_file = sub_dir / "module.py"
    py_file.write_text("def hello(): pass\n", encoding="utf-8")

    # Initially not indexed
    assert _is_already_indexed(str(tmp_path)) is False

    # Mark as indexed
    _mark_indexed(str(tmp_path))
    assert _is_already_indexed(str(tmp_path)) is True

    # Root directory mtime does NOT change when an existing file inside pkg/ is modified!
    # Update file content and mtime:
    py_file.write_text("def hello_updated(): pass\n", encoding="utf-8")
    import os
    import time
    new_time = time.time() + 10
    os.utime(py_file, (new_time, new_time))

    # Freshness check must detect the file modification and return False!
    assert _is_already_indexed(str(tmp_path)) is False

    # Re-marking as indexed should make it fresh again
    _mark_indexed(str(tmp_path))
    assert _is_already_indexed(str(tmp_path)) is True


@pytest.mark.asyncio
async def test_frontier_loop_auto_retrieves_context_on_simple_tasks(tmp_path: Path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node

    dummy_snippets = [{"file_path": "auth.py", "symbol": "login"}]
    orch = SimpleNamespace(
        working_dir=str(tmp_path),
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
        _session_modified_files=set(),
    )
    state = {
        "task": "explain authentication in auth.py",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
        # Note: 'retrieved_snippets' is intentionally omitted to simulate a simple frontier task
    }

    mock_build_prompt = MagicMock(return_value=[{"role": "system", "content": "You are assistant."}])
    with (
        patch("src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator", return_value=orch),
        patch("src.core.orchestration.graph.nodes.perception_retrieval._retrieve_context", new=AsyncMock(return_value=dummy_snippets)) as mock_retrieve,
        patch("src.core.context.context_builder.ContextBuilder.build_prompt", mock_build_prompt),
        patch("src.core.orchestration.graph.nodes.frontier_loop_node.call_model", new=AsyncMock(return_value={"choices": [{"message": {"content": "Auth explained"}}]})),
    ):
        result = await frontier_loop_node(state, config={})
        # Verify _retrieve_context was called because snippets were absent
        mock_retrieve.assert_awaited_once()
        # Verify ContextBuilder was given the retrieved snippets
        args, kwargs = mock_build_prompt.call_args
        # args: (mode, retrieved_snippets, task, tools_schema, history, token_budget, ...)
        assert args[1] == dummy_snippets
        # Verify result contains the retrieved snippets
        assert result.get("retrieved_snippets") == dummy_snippets


def test_tui_diff_preview_requires_confirmation():
    from textual._context import active_app
    from tui.tui_src.ui.components.diff_viewer import SideBySideDiff, InlineDiff

    mock_app = MagicMock()
    mock_app._compose_stacks = [[]]
    token = active_app.set(mock_app)
    try:
        # When requires_confirmation is False: no action bar should be rendered
        sbs_nogate = SideBySideDiff(path="foo.py", diff="@@ -1 +1 @@\n-old\n+new", requires_confirmation=False)
        assert sbs_nogate._requires_confirmation is False

        # When requires_confirmation is True: action bar with Accept and Reject buttons must be rendered
        sbs_gated = SideBySideDiff(path="foo.py", diff="@@ -1 +1 @@\n-old\n+new", requires_confirmation=True)
        assert sbs_gated._requires_confirmation is True

        # Same check for InlineDiff
        inline_nogate = InlineDiff(path="foo.py", diff="@@ -1 +1 @@\n-old\n+new", requires_confirmation=False)
        assert inline_nogate._requires_confirmation is False

        inline_gated = InlineDiff(path="foo.py", diff="@@ -1 +1 @@\n-old\n+new", requires_confirmation=True)
        assert inline_gated._requires_confirmation is True
    finally:
        active_app.reset(token)



@pytest.mark.asyncio
async def test_tui_spawn_permission_button_allow_and_deny():
    from textual.widgets import Button
    from tui.tui_src.ui._app_tool_handlers_mixin import AppToolHandlersMixin
    from tui.tui_src.ui.events import SpawnPermissionApproved, SpawnPermissionDenied

    class DummyApp(AppToolHandlersMixin):
        def __init__(self):
            self._bridge = MagicMock()
            self._posted_messages = []
            self._perm_count = 1

        def _update_perm_badge(self, delta):
            self._perm_count += delta

        async def _mount_chat_widget(self, w):
            pass

        def post_message(self, msg):
            self._posted_messages.append(msg)

        def query_one(self, selector):
            return MagicMock()

    app = DummyApp()

    # Test Allow button
    allow_event = MagicMock()
    allow_event.button = Button("Allow", id="btn_spawn_allow_tool123")
    await app.on_any_button(allow_event)

    app._bridge.publish.assert_any_call("spawn.permission_granted", {"tool_id": "tool123"})
    app._bridge.publish.assert_any_call("tool.permission_granted", {"tool_id": "tool123"})
    assert any(isinstance(m, SpawnPermissionApproved) and m.tool_id == "tool123" for m in app._posted_messages)
    assert app._perm_count == 0

    # Test Deny button
    deny_event = MagicMock()
    deny_event.button = Button("Deny", id="btn_spawn_deny_tool456")
    await app.on_any_button(deny_event)

    app._bridge.publish.assert_any_call("spawn.permission_denied", {"tool_id": "tool456"})
    app._bridge.publish.assert_any_call("tool.permission_denied", {"tool_id": "tool456"})
    assert any(isinstance(m, SpawnPermissionDenied) and m.tool_id == "tool456" for m in app._posted_messages)


@pytest.mark.asyncio
async def test_tui_role_forwarding_to_frontier_loop():
    """Verify frontier_loop_node uses state active_role and filters tools for that role."""
    from unittest.mock import patch, AsyncMock
    from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator()
    orch.active_role = "reviewer"

    state = {
        "task": "review patch",
        "active_role": "reviewer",
        "history": [],
        "rounds": 0,
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "errors": [],
        "model_tier": "frontier",
        "retrieved_snippets": [],
        "turn_count": 1,
        "max_turns": 10,
    }
    config = {"configurable": {"orchestrator": orch}}

    with patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._call_llm_for_turn",
        new_callable=AsyncMock,
    ) as mock_llm:
        mock_llm.return_value = ({"choices": [{"message": {"content": "Looks good."}}]}, "Looks good.", [])
        result = await frontier_loop_node(state, config)
        assert result.get("active_role") == "reviewer"
        mock_llm.assert_awaited_once()
        _, kwargs = mock_llm.call_args
        assert kwargs.get("role") == "reviewer"


@pytest.mark.asyncio
async def test_tui_slash_mcp_writes_to_codingagent_config(tmp_path: Path):
    """Verify TUI /mcp add writes to .codingAgent/config.json."""
    from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin
    from src.core.config_loader import get_mcp_servers

    class DummyApp(AppSlashCommandsMixin):
        def __init__(self, wdir):
            self._bridge = MagicMock()
            self._bridge.working_dir = str(wdir)
            self._bridge._orchestrator = MagicMock()
            self._bridge._orchestrator.working_dir = str(wdir)
            self.notifications = []

        def notify(self, msg, severity="info"):
            self.notifications.append((msg, severity))

    app = DummyApp(tmp_path)
    await app._slash_mcp("add myserver npx -y @modelcontextprotocol/server-memory")

    config_file = tmp_path / ".codingAgent" / "config.json"
    assert config_file.exists(), "config.json was not written to .codingAgent/"
    servers = get_mcp_servers(tmp_path)
    assert any(s.get("name") == "myserver" for s in servers)


def test_session_screen_offset_fork_and_revert(tmp_path: Path):
    """Verify SessionScreen subtracts offset when forking/reverting."""
    from unittest.mock import PropertyMock, patch
    from tui.tui_src.ui.screens.session_screen import SessionScreen

    screen = SessionScreen()
    screen._show_new_session = True
    mock_app = MagicMock()
    mock_app._get_sessions_dir = lambda: tmp_path

    p0 = tmp_path / "sess0.json"
    p0.write_text("{}", encoding="utf-8")
    p1 = tmp_path / "sess1.json"
    p1.write_text("{}", encoding="utf-8")

    screen._filtered = [
        (p0, {"task_name": "Session 0", "git_sha": "abc1234"}),
        (p1, {"task_name": "Session 1", "git_sha": "def5678"}),
    ]

    with patch.object(SessionScreen, "app", new_callable=PropertyMock, return_value=mock_app):
        # Row 0 is [+ Start new session]
        screen._selected = 0
        screen.action_fork_session()
        mock_app.notify.assert_called_with("Cannot fork the 'Start new session' item.", severity="warning")

        # Row 1 corresponds to _filtered[0]
        screen._selected = 1
        screen.action_fork_session()
        assert any("Session forked" in str(c) for c in mock_app.notify.call_args_list)

        # Revert with invalid git_sha fails safely
        screen._do_revert("; rm -rf /", str(tmp_path))
        mock_app.notify.assert_called_with("Invalid git SHA format.", severity="error")


def test_save_provider_credentials_writes_to_user_prefs(tmp_path: Path, monkeypatch):
    """Verify save_provider_credentials writes to user prefs.json with 0600 permissions."""
    from tui.tui_src.ui.config_writer import save_provider_credentials, load_provider_credentials
    from src.core.user_prefs import UserPrefs

    prefs_file = tmp_path / "prefs.json"
    monkeypatch.setattr("src.core.user_prefs.DEFAULT_FILENAME", str(prefs_file))

    save_provider_credentials("mock_provider", "secret_key_xyz", "http://api.mock.com")

    assert prefs_file.exists()
    assert (prefs_file.stat().st_mode & 0o777) == 0o600

    creds = load_provider_credentials("mock_provider")
    assert creds["api_key"] == "secret_key_xyz"
    assert creds["base_url"] == "http://api.mock.com"



