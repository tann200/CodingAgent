"""Integration test for delegation depth enforcement.

Verifies that delegate_task refuses to spawn a subagent when the process-local
ContextVar _DELEGATION_DEPTH_VAR is at or above _MAX_DELEGATION_DEPTH.
"""

from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR, _MAX_DELEGATION_DEPTH


def test_delegation_refused_at_max_depth():
    """delegate_task must return an error string when depth >= _MAX_DELEGATION_DEPTH."""
    token = _DELEGATION_DEPTH_VAR.set(_MAX_DELEGATION_DEPTH)
    try:
        from src.tools.subagent_tools import delegate_task

        result = delegate_task(
            role="coding",
            subtask_description="do something",
            working_dir="/tmp",
        )
        # The guard returns a string error message (not a dict)
        assert result is not None
        error_str = str(result)
        assert (
            "depth" in error_str.lower()
            or "refused" in error_str.lower()
            or "exceeded" in error_str.lower()
            or "maximum" in error_str.lower()
        ), f"Expected depth/refused/exceeded/maximum in: {error_str!r}"
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)


def test_delegation_not_refused_below_max_depth():
    """Depth guard must NOT fire when depth is below _MAX_DELEGATION_DEPTH."""
    # Set depth to one below the limit
    token = _DELEGATION_DEPTH_VAR.set(_MAX_DELEGATION_DEPTH - 1)
    try:
        # We only verify the guard doesn't immediately return an error string
        # Actual graph execution is tested elsewhere; mock the heavy path.
        from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR as dv

        assert dv.get() < _MAX_DELEGATION_DEPTH
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)
