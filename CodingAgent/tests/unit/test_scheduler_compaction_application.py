import time
from src.core.orchestration.event_bus import get_event_bus


def test_scheduler_applies_compacted_history_and_publishes(monkeypatch):
    bus = get_event_bus()
    events = {"compaction_applied": False}

    # Stub distill_context to return a compacted history
    def fake_distill(messages, working_dir=None, **kwargs):
        # Return a compacted history with a single system summary + continuation
        return {
            "current_task": "x",
            "current_state": "y",
            "next_step": "z",
            "_compacted_history": [
                {"role": "system", "content": "[COMPACTED] summary"},
                {"role": "user", "content": "Continue from summary"},
            ],
        }

    monkeypatch.setattr("src.core.memory.distiller.distill_context", fake_distill)

    def on_compaction(payload):
        events["compaction_applied"] = True

    bus.subscribe("message.compaction_applied", on_compaction)

    # Register the subscription handler by invoking the bootstrap helper
    import src.core.orchestration.orchestrator_bootstrap as ob

    class _FakeMsgMgr:
        def __init__(self):
            # start with some dummy messages
            self.messages = [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]

    class _FakeOrch:
        def __init__(self, bus):
            self.event_bus = bus
            self.msg_mgr = _FakeMsgMgr()
            self.working_dir = None

    fo = _FakeOrch(bus)
    ob._init_event_subscriptions(fo)

    # Publish the request and wait briefly for background thread to run
    bus.publish("scheduler.distill_request", {"test": True})
    time.sleep(0.5)

    # Assert that the compaction was applied to the MessageManager
    assert isinstance(fo.msg_mgr.messages, list)
    assert fo.msg_mgr.messages[0]["role"] == "system"
    assert events["compaction_applied"] is True
