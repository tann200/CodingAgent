import time
from src.core.orchestration.event_bus import get_event_bus


def test_scheduler_compaction_integration_end_to_end(monkeypatch, tmp_path):
    """Integration-style test: ensure distillation compacts long histories,
    applies the compacted history into MessageManager, and publishes token
    metrics in the compaction event payload.
    """
    bus = get_event_bus()

    # Force the compaction token threshold very low so compaction triggers
    monkeypatch.setattr("src.core.config_loader.get", lambda k, d=None: 1)

    # Replace compact_messages_to_prose so we don't call an external LLM.
    monkeypatch.setattr(
        "src.core.memory.distiller.compact_messages_to_prose",
        lambda messages, working_dir=None: "[FAKE SUMMARY]",
    )

    # Stub the LLM call used by distill_context (the JSON distillation step)
    def _fake_llm_sync(messages, format_json=False, **kwargs):
        # Return a deterministic JSON blob the distiller expects
        return '{"current_task": "t", "current_state": "s", "next_step": "n"}'

    monkeypatch.setattr("src.core.memory.distiller._call_llm_sync", _fake_llm_sync)

    import src.core.orchestration.orchestrator_bootstrap as ob

    class _FakeMsgMgr:
        def __init__(self):
            self.messages = []

    class _FakeOrch:
        def __init__(self, bus):
            self.event_bus = bus
            self.msg_mgr = _FakeMsgMgr()
            self.working_dir = tmp_path
            # no lock present to emulate tests that don't call full bootstrap
            self._msg_mgr_lock = None

    fo = _FakeOrch(bus)

    # Populate a large message history (100 messages, 1000 chars each)
    original_msgs = [{"role": "user", "content": "x" * 1000} for _ in range(100)]
    fo.msg_mgr.messages = list(original_msgs)

    # Capture the compaction event payload
    captured = {}

    def on_compaction(payload):
        captured["payload"] = payload

    bus.subscribe("message.compaction_applied", on_compaction)

    # Register subscriptions and trigger the scheduler distill request
    ob._init_event_subscriptions(fo)

    orig_count = len(original_msgs)
    bus.publish("scheduler.distill_request", {"test": True})

    # Wait for background worker to complete (poll up to 5s)
    deadline = time.time() + 5.0
    while time.time() < deadline and "payload" not in captured:
        time.sleep(0.1)

    # The MessageManager must now contain the compacted history (system summary + recent + continuation)
    assert isinstance(fo.msg_mgr.messages, list)
    # compaction inserts a system message as the first element
    assert fo.msg_mgr.messages[0]["role"] == "system"

    # Event payload must have been published and include token metrics
    assert "payload" in captured
    payload = captured["payload"]
    assert payload.get("original_count") == orig_count
    assert payload.get("new_count") == len(fo.msg_mgr.messages)
    assert payload.get("dropped_count") == max(0, orig_count - payload.get("new_count"))

    # Token metrics are best-effort integers (or None); if present they must be consistent
    orig_tok = payload.get("original_tokens")
    new_tok = payload.get("new_tokens")
    tokens_reduced = payload.get("tokens_reduced")
    if orig_tok is not None and new_tok is not None and tokens_reduced is not None:
        assert isinstance(orig_tok, int) or isinstance(orig_tok, float)
        assert isinstance(new_tok, int) or isinstance(new_tok, float)
        assert tokens_reduced == orig_tok - new_tok
