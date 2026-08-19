"""
Contract tests for Fast-Path stabilization: sequenced event delivery.

Phase 1 sequenced dispatch guarantees:
  1. Tool lifecycle events (ToolExecuteStart -> ToolExecuteFinish/Error)
     are delivered in FIFO publication order within the "tool" category.
  2. Step lifecycle events (StepStart -> StepFinish) are delivered in
     FIFO publication order within the "step" category.
  3. Delegation lifecycle events (DelegationStart -> DelegationFinish)
     are delivered in FIFO publication order within the "delegation" category.
  4. Categories are independent: a slow tool event does not block delegation.
  5. Non-sequenced events are dispatched concurrently (no ordering guarantee).
"""

import threading
from typing import List

import pytest

from src.core.messaging.bus import MessageBus
from src.core.messaging.event_types import (
    DelegationFinish,
    DelegationStart,
    StepFinish,
    StepStart,
    ToolExecuteError,
    ToolExecuteFinish,
    ToolExecuteStart,
)
from src.core.messaging.events import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class OrderTracker:
    """Records the type-order in which events are received."""

    def __init__(self, expected: int = 0):
        self.received: List[Event] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._expected = expected

    def handle(self, event: Event) -> None:
        with self._lock:
            self.received.append(event)
            if self._expected > 0 and len(self.received) >= self._expected:
                self._done.set()

    def wait(self, timeout: float = 5.0) -> bool:
        return self._done.wait(timeout=timeout)

    @property
    def order(self) -> List[type]:
        with self._lock:
            return [type(e) for e in self.received]


# Minimal event instances for contract tests
_TOOL_START = ToolExecuteStart(tool="test", args={}, tool_call_id="tc1")
_TOOL_FINISH = ToolExecuteFinish(
    session_update={}, tool_call_id="tc1", title="test",
    status="completed", content=None, raw_output=None, workdir="/tmp",
)
_TOOL_ERROR = ToolExecuteError(
    session_update={}, tool_call_id="tc1", title="test",
    status="failed", content=None, error="fail", workdir="/tmp",
)
_STEP_START = StepStart(step=1, total=3, tool="test", description="step 1",
                        session_id="s1")
_STEP_FINISH = StepFinish(step=1, total=3, tool="test", ok=True,
                          elapsed_ms=100.0, tool_call_count=1, session_id="s1")
_DELEG_START = DelegationStart(child_session_id="child1",
                               parent_session_id="parent1",
                               role="analyst", task="analyze")
_DELEG_FINISH = DelegationFinish(child_session_id="child1",
                                 role="analyst", ok=True, cost_usd=0.0)


@pytest.fixture
def bus():
    b = MessageBus(max_queue_size=200, worker_threads=1)
    yield b
    b.shutdown(timeout=3.0)


# ---------------------------------------------------------------------------
# Category: tool
# ---------------------------------------------------------------------------

class TestToolEventOrdering:
    """Phase 1: ToolExecuteStart -> ToolExecuteFinish/Error in FIFO order."""

    def test_tool_start_then_finish_in_order(self, bus):
        tracker = OrderTracker(expected=2)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)

        bus.publish(_TOOL_START)
        bus.publish(_TOOL_FINISH)

        assert tracker.wait()
        assert tracker.order == [ToolExecuteStart, ToolExecuteFinish]

    def test_tool_start_then_error_in_order(self, bus):
        tracker = OrderTracker(expected=2)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteError, tracker)

        bus.publish(_TOOL_START)
        bus.publish(_TOOL_ERROR)

        assert tracker.wait()
        assert tracker.order == [ToolExecuteStart, ToolExecuteError]

    def test_multiple_tool_pairs_in_order(self, bus):
        tracker = OrderTracker(expected=4)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)

        bus.publish(_TOOL_START)
        bus.publish(_TOOL_FINISH)
        bus.publish(_TOOL_START)
        bus.publish(_TOOL_FINISH)

        assert tracker.wait()
        assert tracker.order == [
            ToolExecuteStart, ToolExecuteFinish,
            ToolExecuteStart, ToolExecuteFinish,
        ]

    def test_shutdown_drains_ordered_tool_events(self):
        bus = MessageBus(max_queue_size=8, worker_threads=1)
        tracker = OrderTracker(expected=4)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)

        bus.publish(_TOOL_START)
        bus.publish(_TOOL_FINISH)
        bus.publish(_TOOL_START)
        bus.publish(_TOOL_FINISH)
        bus.shutdown(timeout=3.0)

        assert tracker.order == [
            ToolExecuteStart,
            ToolExecuteFinish,
            ToolExecuteStart,
            ToolExecuteFinish,
        ]


# ---------------------------------------------------------------------------
# Category: step
# ---------------------------------------------------------------------------

class TestStepEventOrdering:
    """Phase 1: StepStart -> StepFinish in FIFO order."""

    def test_step_start_then_finish_in_order(self, bus):
        tracker = OrderTracker(expected=2)
        bus.subscribe(StepStart, tracker)
        bus.subscribe(StepFinish, tracker)

        bus.publish(_STEP_START)
        bus.publish(_STEP_FINISH)

        assert tracker.wait()
        assert tracker.order == [StepStart, StepFinish]

    def test_multiple_step_pairs_in_order(self, bus):
        tracker = OrderTracker(expected=4)
        bus.subscribe(StepStart, tracker)
        bus.subscribe(StepFinish, tracker)

        step2_finish = StepFinish(
            step=2, total=3, tool="test", ok=True,
            elapsed_ms=50.0, tool_call_count=0, session_id="s1",
        )
        step2_start = StepStart(
            step=2, total=3, tool="test", description="step 2",
            session_id="s1",
        )

        bus.publish(_STEP_START)
        bus.publish(_STEP_FINISH)
        bus.publish(step2_start)
        bus.publish(step2_finish)

        assert tracker.wait()
        assert tracker.order == [
            StepStart, StepFinish,
            StepStart, StepFinish,
        ]


# ---------------------------------------------------------------------------
# Category: delegation
# ---------------------------------------------------------------------------

class TestDelegationEventOrdering:
    """Phase 1: DelegationStart -> DelegationFinish in FIFO order."""

    def test_delegation_start_then_finish_in_order(self, bus):
        tracker = OrderTracker(expected=2)
        bus.subscribe(DelegationStart, tracker)
        bus.subscribe(DelegationFinish, tracker)

        bus.publish(_DELEG_START)
        bus.publish(_DELEG_FINISH)

        assert tracker.wait()
        assert tracker.order == [DelegationStart, DelegationFinish]

    def test_multiple_delegation_pairs_in_order(self, bus):
        tracker = OrderTracker(expected=4)
        bus.subscribe(DelegationStart, tracker)
        bus.subscribe(DelegationFinish, tracker)

        bus.publish(_DELEG_START)
        bus.publish(_DELEG_FINISH)
        bus.publish(_DELEG_START)
        bus.publish(_DELEG_FINISH)

        assert tracker.wait()
        assert tracker.order == [
            DelegationStart, DelegationFinish,
            DelegationStart, DelegationFinish,
        ]


# ---------------------------------------------------------------------------
# Cross-category independence
# ---------------------------------------------------------------------------

class TestCrossCategoryIndependence:
    """Phase 1: Categories are independent — tool does not block delegation."""

    def test_tool_and_delegation_interleave(self, bus):
        tool_tracker = OrderTracker(expected=2)
        deleg_tracker = OrderTracker(expected=2)

        bus.subscribe(ToolExecuteStart, tool_tracker)
        bus.subscribe(ToolExecuteFinish, tool_tracker)
        bus.subscribe(DelegationStart, deleg_tracker)
        bus.subscribe(DelegationFinish, deleg_tracker)

        bus.publish(_TOOL_START)
        bus.publish(_DELEG_START)
        bus.publish(_TOOL_FINISH)
        bus.publish(_DELEG_FINISH)

        assert tool_tracker.wait()
        assert deleg_tracker.wait()
        assert tool_tracker.order == [ToolExecuteStart, ToolExecuteFinish]
        assert deleg_tracker.order == [DelegationStart, DelegationFinish]

    def test_interleaved_tool_ordering_preserved(self, bus):
        tracker = OrderTracker(expected=4)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)

        bus.publish(_TOOL_START)
        bus.publish(_DELEG_START)
        bus.publish(_TOOL_FINISH)
        bus.publish(_TOOL_START)
        bus.publish(_DELEG_FINISH)
        bus.publish(_TOOL_FINISH)

        assert tracker.wait()
        assert tracker.order == [
            ToolExecuteStart, ToolExecuteFinish,
            ToolExecuteStart, ToolExecuteFinish,
        ]

    def test_all_three_categories_interleaved(self, bus):
        tool_tracker = OrderTracker(expected=2)
        step_tracker = OrderTracker(expected=2)
        deleg_tracker = OrderTracker(expected=2)

        bus.subscribe(ToolExecuteStart, tool_tracker)
        bus.subscribe(ToolExecuteFinish, tool_tracker)
        bus.subscribe(StepStart, step_tracker)
        bus.subscribe(StepFinish, step_tracker)
        bus.subscribe(DelegationStart, deleg_tracker)
        bus.subscribe(DelegationFinish, deleg_tracker)

        bus.publish(_TOOL_START)
        bus.publish(_STEP_START)
        bus.publish(_DELEG_START)
        bus.publish(_TOOL_FINISH)
        bus.publish(_STEP_FINISH)
        bus.publish(_DELEG_FINISH)

        assert tool_tracker.wait()
        assert step_tracker.wait()
        assert deleg_tracker.wait()
        assert tool_tracker.order == [ToolExecuteStart, ToolExecuteFinish]
        assert step_tracker.order == [StepStart, StepFinish]
        assert deleg_tracker.order == [DelegationStart, DelegationFinish]
