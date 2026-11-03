"""Unit tests for SPEC-P1.2: Cancellation Settlement Guard commit leak fix in verification_node and finalize_node."""

import pytest
import threading
from unittest.mock import MagicMock, AsyncMock

from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.nodes.finalize_node import finalize_node


@pytest.mark.asyncio
async def test_verification_node_returns_passed_false_when_cancelled():
    """SPEC-P1.2: verification_node must report verification_passed=False when cancelled."""
    cancel_ev = threading.Event()
    cancel_ev.set()

    state = {
        "cancel_event": cancel_ev,
        "modified_files": ["foo.py"],
        "working_dir": ".",
        "turn_count": 1,
    }
    config = {"configurable": {}}

    res = await verification_node(state, config)
    assert res.get("verification_passed") is False
    assert res.get("verification_result", {}).get("cancelled") is True


@pytest.mark.asyncio
async def test_finalize_node_rolls_back_when_cancelled_even_if_verify_was_skipped_or_marked_passed():
    """SPEC-P1.2: finalize_node must roll back open step transactions on cancellation and never commit."""
    cancel_ev = threading.Event()
    cancel_ev.set()

    orch = MagicMock()
    orch._step_snapshot_id = "snap-test-123"
    orch.rollback_step_transaction = MagicMock(return_value={"restored_count": 1})
    orch.commit_step_transaction = MagicMock()

    state = {
        "cancel_event": cancel_ev,
        "verification_passed": True,  # even if erroneously passed
        "run": {"status": "cancelled", "outcome": "cancelled"},
        "_orchestrator": orch,
    }
    config = {"configurable": {"orchestrator": orch}}

    res = await finalize_node(state, config)
    orch.rollback_step_transaction.assert_called_once()
    orch.commit_step_transaction.assert_not_called()
    assert res["run"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_finalize_node_commits_when_verified_and_not_cancelled():
    """SPEC-P1.2: finalize_node commits open step transaction when verification passed and uncancelled."""
    cancel_ev = threading.Event()

    orch = MagicMock()
    orch._step_snapshot_id = "snap-test-456"
    orch.rollback_step_transaction = MagicMock()
    orch.commit_step_transaction = MagicMock()

    state = {
        "cancel_event": cancel_ev,
        "verification_passed": True,
        "run": {"status": "running", "outcome": "complete"},
        "_orchestrator": orch,
    }
    config = {"configurable": {"orchestrator": orch}}

    res = await finalize_node(state, config)
    orch.commit_step_transaction.assert_called_once()
    orch.rollback_step_transaction.assert_not_called()
    assert res["run"]["status"] == "completed"
