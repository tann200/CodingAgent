import time

from src.tools import _diff_gate as diff_gate


def test_pre_resolve_ttl_expires():
    """Pre-resolved results should expire according to TTL so that a
    resolver that ran far in the past does not permanently satisfy a later
    registrant.
    """
    key = "ttl-expiry-test"

    # Clean state
    diff_gate.reset_preview_gate()

    # Use a tiny TTL so test runs fast
    diff_gate.set_preview_result_ttl(0.02)

    # Pre-resolve as rejected
    diff_gate.resolve_preview_gate(key, approved=False)

    # Wait long enough for TTL to expire
    time.sleep(0.05)

    # Now register — the pre-resolve should have expired, so the returned
    # Event must not be already set and no rejection should be recorded.
    ev = diff_gate.register_preview_gate(key)
    assert not ev.is_set()
    assert not diff_gate.is_preview_rejected(key)
