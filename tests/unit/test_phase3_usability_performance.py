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


