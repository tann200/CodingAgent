import threading

from src.tools import _diff_gate as diff_gate


def test_pre_resolve_then_register():
    """If resolve_preview_gate runs before register_preview_gate, the
    subsequent registrant must observe the prior decision immediately.
    """
    key = "pre-resolve-test"

    # Clean state
    diff_gate.reset_preview_gate()

    # Resolve before registering (simulate EventBus handler arriving early)
    diff_gate.resolve_preview_gate(key, approved=False)

    # Now register — should return an Event that's already set and the
    # rejected set should contain the key.
    ev = diff_gate.register_preview_gate(key)
    assert ev.is_set()
    assert diff_gate.is_preview_rejected(key)
