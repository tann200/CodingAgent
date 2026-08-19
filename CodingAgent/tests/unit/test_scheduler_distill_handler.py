import time
from src.core.orchestration.event_bus import get_event_bus


def test_distill_handler_invokes_distiller_and_publishes(monkeypatch):
    bus = get_event_bus()
    called = {"distill": False, "completed": False, "failed": False}

    # Stub distill_context to mark as called
    def fake_distill(messages, working_dir=None, **kwargs):
        called["distill"] = True
        return {"current_task": "x", "current_state": "y", "next_step": "z"}

    monkeypatch.setattr("src.core.memory.distiller.distill_context", fake_distill)

    def on_completed(payload):
        called["completed"] = True

    def on_failed(payload):
        called["failed"] = True

    bus.subscribe("scheduler.distill_completed", on_completed)
    bus.subscribe("scheduler.distill_failed", on_failed)

    # Register the subscription handler by invoking the bootstrap helper
    # that attaches event subscriptions. Provide a minimal orch-like object.
    import src.core.orchestration.orchestrator_bootstrap as ob

    class _FakeOrch:
        def __init__(self, bus):
            self.event_bus = bus
            self.msg_mgr = type("M", (), {"messages": []})()
            self.working_dir = None

    fo = _FakeOrch(bus)
    ob._init_event_subscriptions(fo)

    # Publish the request and wait briefly for background thread to run
    bus.publish("scheduler.distill_request", {"test": True})
    time.sleep(0.5)

    assert called["distill"] is True
    assert called["completed"] is True
    assert called["failed"] is False
