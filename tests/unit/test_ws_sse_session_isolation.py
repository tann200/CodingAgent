import asyncio
import pytest
from fastapi.testclient import TestClient

from src.core.orchestration.event_bus import EventBus, set_session_id
from src.server.app import app, register_event_bus
from src.server.sse_adapter import ServerEventBusAdapter
from src.server.metrics import inc_client_event_dropped_counter, _CLIENT_DROPPED_EVENT_COUNTERS, _MAX_CLIENT_DROPPED_COUNTERS


def test_ws_session_isolation_and_context_propagation():
    """Verify WebSocket clients only receive events targeted to their session,

    and events published within a session context automatically receive session_id.
    """
    bus = EventBus()

    with TestClient(app) as client:
        register_event_bus(bus)

        with client.websocket_connect("/ws/session/session_a") as ws_a:
            with client.websocket_connect("/ws/session/session_b") as ws_b:
                # Publish event inside session_a context
                set_session_id("session_a")
                try:
                    bus.publish("tool.execute.start", {"tool": "read_file", "path": "test.txt"})
                finally:
                    set_session_id(None)

                # Client A receives the event
                msg_a = ws_a.receive_json()
                assert msg_a["event"] == "tool.execute.start"
                assert msg_a["data"]["session_id"] == "session_a"
                assert msg_a["data"]["tool"] == "read_file"

                # Client B should NOT receive anything; publish ping to verify client B is alive
                ws_b.send_json({"type": "ping"})
                msg_b = ws_b.receive_json()
                assert msg_b["event"] == "_control"
                assert msg_b["data"]["type"] == "pong"


@pytest.mark.asyncio
async def test_sse_session_isolation():
    """Verify SSE generator only yields events for the targeted session."""
    bus = EventBus()
    adapter = ServerEventBusAdapter(bus)

    gen_a = adapter.event_generator("session_a")
    gen_b = adapter.event_generator("session_b")

    # Start generator tasks
    task_a = asyncio.create_task(gen_a.__anext__())
    task_b = asyncio.create_task(gen_b.__anext__())
    await asyncio.sleep(0.02)

    # Publish event inside session_a context
    set_session_id("session_a")
    try:
        bus.publish("tool.execute.finish", {"tool": "write_file", "status": "ok"})
    finally:
        set_session_id(None)

    # gen_a yields the event
    res_a = await asyncio.wait_for(task_a, timeout=1.0)
    assert "tool.execute.finish" in res_a
    assert "session_a" in res_a

    # gen_b should not have completed
    assert not task_b.done()

    # Now publish inside session_b context to wake up task_b
    set_session_id("session_b")
    try:
        bus.publish("tool.execute.finish", {"tool": "read_file", "status": "ok"})
    finally:
        set_session_id(None)

    res_b = await asyncio.wait_for(task_b, timeout=1.0)
    assert "tool.execute.finish" in res_b
    assert "session_b" in res_b

    await gen_a.aclose()
    await gen_b.aclose()


def test_ws_subscription_capping():
    """Verify WebSocket subscribe commands are capped at 32 subscriptions."""
    bus = EventBus()

    with TestClient(app) as client:
        register_event_bus(bus)

        # Connect with no initial events
        with client.websocket_connect("/ws/session/session_cap?events=none") as ws:
            # Subscribe up to 32 events
            for i in range(32):
                ws.send_json({"type": "subscribe", "event": f"custom.event.{i}"})
                resp = ws.receive_json()
                assert resp["event"] == "_control"
                assert resp["data"]["type"] == "subscribed"
                assert resp["data"]["event"] == f"custom.event.{i}"

            # 33rd subscription must fail with error
            ws.send_json({"type": "subscribe", "event": "custom.event.33"})
            resp = ws.receive_json()
            assert resp["event"] == "_control"
            assert resp["data"]["type"] == "error"


def test_client_dropped_event_counters_bounding():
    """Verify _CLIENT_DROPPED_EVENT_COUNTERS does not exceed 1000 entries."""
    for i in range(1200):
        inc_client_event_dropped_counter(f"event_{i}", f"session_{i}")

    assert len(_CLIENT_DROPPED_EVENT_COUNTERS) <= _MAX_CLIENT_DROPPED_COUNTERS
