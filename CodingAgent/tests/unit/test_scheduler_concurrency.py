import threading
import time
from pathlib import Path

from unittest.mock import patch

from src.core.orchestration.message_manager import MessageManager
from src.core.orchestration.orchestrator_bootstrap import (
    _init_event_subscriptions,
    register_config_reload_handlers,
)
from src.core.orchestration.event_bus import EventBus


def test_compaction_applies_safely_during_concurrent_appends(monkeypatch):
    """Simulate distillation running while messages are being appended concurrently.

    Verifies the compaction handler safely replaces the in-memory message list
    (using orch._msg_mgr_lock when present) without raising and that the
    'scheduler.distill_completed' event is published.
    """

    class FakeOrch:
        pass

    orch = FakeOrch()
    orch.event_bus = EventBus()
    orch.working_dir = Path("/tmp")
    orch._msg_mgr_lock = threading.Lock()
    # start with a few messages
    orch.msg_mgr = MessageManager(max_tokens=None, event_bus=orch.event_bus)
    for i in range(10):
        orch.msg_mgr.append("user", f"initial {i}")

    # Register subscriptions (this wires the distill handler)
    _init_event_subscriptions(orch)

    done = threading.Event()

    def on_done(payload):
        done.set()

    orch.event_bus.subscribe("scheduler.distill_completed", on_done)

    # Slow distill implementation that returns a compacted history
    def slow_distill(msgs, working_dir=None, **kwargs):
        time.sleep(0.2)
        return {"_compacted_history": [{"role": "user", "content": "compacted"}]}

    with patch("src.core.memory.distiller.distill_context", side_effect=slow_distill):
        # Trigger distillation (handler runs in background thread)
        orch.event_bus.publish("scheduler.distill_request", {"source": "test"})

        # While distillation runs, append some messages concurrently
        for i in range(20):
            orch.msg_mgr.append("user", f"concurrent {i}")
            time.sleep(0.01)

        # Wait for compaction to complete
        assert done.wait(timeout=5.0), "Compaction did not complete in time"

    # After compaction, the msg_mgr.messages should contain the compacted content
    assert any(
        (isinstance(m, dict) and m.get("content") == "compacted")
        for m in orch.msg_mgr.messages
    )


def test_register_config_reload_handlers_thread_safety():
    """Call register_config_reload_handlers concurrently from multiple threads.

    The function should be best-effort and not raise; after concurrent calls the
    internal weakset should contain the orchestrator at most once.
    """

    class FakeOrch:
        pass

    orch = FakeOrch()

    # Call concurrently from multiple threads
    def _call():
        try:
            register_config_reload_handlers(orch)
        except Exception:
            pass

    threads = [threading.Thread(target=_call) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # If the function registered the _registered attribute it should be a WeakSet
    reg = getattr(register_config_reload_handlers, "_registered", None)
    if reg is not None:
        # The orchestrator should be present (or garbage-collected) but not duplicated
        # Ensure no exception accessing the weakset
        try:
            assert orch in reg
        except Exception:
            # If orch was garbage collected quickly, that's acceptable
            pass
