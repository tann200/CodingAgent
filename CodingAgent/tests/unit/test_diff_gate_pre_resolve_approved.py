
from src.tools import _diff_gate as diff_gate


def test_pre_resolve_then_register_approved():
    """If resolve_preview_gate runs before register_preview_gate with
    approved=True, the subsequent registrant must observe the prior
    decision immediately and not record a rejection.
    """
    key = "pre-resolve-test-approved"

    # Clean state
    diff_gate.reset_preview_gate()

    # Resolve before registering (simulate EventBus handler arriving early)
    diff_gate.resolve_preview_gate(key, approved=True)

    # Now register — should return an Event that's already set and no
    # rejection should be recorded.
    ev = diff_gate.register_preview_gate(key)
    assert ev.is_set()
    assert not diff_gate.is_preview_rejected(key)
