"""tests/integration/test_delegation_depth.py — HR-5 depth enforcement

Verifies that delegate_task refuses to spawn subagents when
delegation depth reaches the configured maximum, preventing
unbounded recursive subagent spawning (P2-T1).
"""


# ruff: noqa: E501
from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def test_delegation_refused_at_max_depth():
    """delegate_task must return an error string when depth >= _MAX_DELEGATION_DEPTH."""
    from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR, _MAX_DELEGATION_DEPTH

    token = _DELEGATION_DEPTH_VAR.set(_MAX_DELEGATION_DEPTH)
    try:
        from src.tools.subagent_tools import delegate_task

        result = delegate_task(
            role="coding",
            subtask_description="test subtask",
            working_dir="/tmp",
        )
        assert isinstance(result, str)
        assert "depth" in result.lower() or "refusing" in result.lower()
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)


def test_delegation_allowed_below_max_depth():
    """delegate_task must not be blocked when depth < _MAX_DELEGATION_DEPTH.

    The guard returns early before any heavy work, so we only verify
    that the function does NOT return the depth error string — we mock
    downstream dependencies to keep the test fast.
    """
    from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR, _MAX_DELEGATION_DEPTH

    assert _MAX_DELEGATION_DEPTH > 0
    token = _DELEGATION_DEPTH_VAR.set(0)
    try:
        from src.tools.subagent_tools import delegate_task

        with patch(
            "src.tools.subagent_tools.get_agent_brain_manager", return_value=None
        ):
            result = delegate_task(
                role="coding",
                subtask_description="test subtask",
                working_dir="/tmp",
            )
        assert "depth" not in str(result).lower()
        assert "refusing" not in str(result).lower()
    finally:
        _DELEGATION_DEPTH_VAR.reset(token)


def test_depth_var_independent_per_context():
    """ContextVar depth must be isolated to each context (thread/asyncio).

    Setting depth in one context must not affect another.
    """
    from src.tools.subagent_tools import _DELEGATION_DEPTH_VAR, _MAX_DELEGATION_DEPTH
    import contextvars

    ctx1 = contextvars.copy_context()
    ctx2 = contextvars.copy_context()

    def _set_and_check():
        _DELEGATION_DEPTH_VAR.set(_MAX_DELEGATION_DEPTH)
        assert _DELEGATION_DEPTH_VAR.get() == _MAX_DELEGATION_DEPTH

    def _check_default():
        assert _DELEGATION_DEPTH_VAR.get() == 0

    ctx1.run(_set_and_check)
    ctx2.run(_check_default)
