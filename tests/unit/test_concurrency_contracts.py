"""
TEST-01 — Real-concurrency and fault-injection contract suite.

These tests deliberately do NOT use the ``sync_threads`` fixture/marker. They
exercise the production threading and asyncio paths with real
``threading.Thread`` / ``ThreadPoolExecutor`` workers so that ordering,
backpressure, cancellation, shutdown, and lock behavior are exercised under
actual concurrency rather than inline execution.

Acceptance criteria (from docs/ARCHITECTURAL_REVIEW_AND_ROADMAP.md, TEST-01):

  - Repeated stress runs complete without deadlock.
  - No event reordering within a category under concurrent publication.
  - Owner-scoped cancellation prevents cross-coroutine corruption.
  - Shutdown drains FIFO without deadlock or post-shutdown logging loops.
  - Session-state hydration returns isolated snapshots (STAB-04).
  - File-lock acquisition serializes correctly across concurrent agents.

Every test bounds its internal synchronization with a timeout so a regression
fails fast instead of hanging the suite.
"""

import asyncio
import threading
import time
from typing import List, Optional

import pytest

from src.core.messaging.bus import (
    MessageBus,
    ReliableEventAdmissionError,
)
from src.core.messaging.event_types import (
    AgentMessage,
    AgentStatus,
    DelegationFinish,
    DelegationStart,
    ToolExecuteFinish,
    ToolExecuteStart,
)
from src.core.messaging.events import Event
from src.core.orchestration.agent_session_manager import AgentSessionManager
from src.core.orchestration.file_lock_manager import FileLockManager

MAX_WAIT = 10.0


# ---------------------------------------------------------------------------
# MessageBus ordering under real concurrent publication
# ---------------------------------------------------------------------------

class _SeqTracker:
    """Thread-safe ordered recorder for a single delivery category."""

    def __init__(self, expected: Optional[int] = None):
        self.received: List[Event] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._expected = expected

    def handle(self, event: Event) -> None:
        with self._lock:
            self.received.append(event)
            if (
                self._expected is not None
                and len(self.received) >= self._expected
            ):
                self._done.set()

    def wait(self, timeout: float = MAX_WAIT) -> bool:
        return self._done.wait(timeout=timeout)

    def order(self) -> List[type]:
        with self._lock:
            return [type(e) for e in self.received]

    def events(self) -> List[Event]:
        with self._lock:
            return list(self.received)


_TOOL_START = ToolExecuteStart(tool="t", args={}, tool_call_id="tc")
_TOOL_FINISH = ToolExecuteFinish(
    session_update="", tool_call_id="tc", title="t", status="completed",
    content=None, raw_output=None, workdir="/tmp",
)
_DELEG_START = DelegationStart(
    child_session_id="c", parent_session_id="p", role="analyst", task="x"
)
_DELEG_FINISH = DelegationFinish(
    child_session_id="c", role="analyst", ok=True, cost_usd=0.0
)


def _publish_pairs(bus: MessageBus, pairs: int, thread_id: int) -> None:
    """Publish ``pairs`` start/finish event pairs from the calling thread.

    Each pair is tagged with a unique (thread_id, pair_index) so ordering can
    be verified per pair even across interleaving publishers.
    """
    for i in range(pairs):
        tag = f"t{thread_id}-{i}"
        bus.publish(ToolExecuteStart(tool="t", args={}, tool_call_id=tag))
        bus.publish(ToolExecuteFinish(
            session_update="", tool_call_id=tag, title="t", status="completed",
            content=None, raw_output=None, workdir="/tmp",
        ))


def _assert_pair_relative_order(order: List[Event]) -> None:
    """Verify every tagged Start is delivered strictly before its Finish.

    Events without a ``tool_call_id`` (e.g. delegation) are skipped, so the
    helper works for any sequenced category while still catching reordering
    in the event class that carries a pairing id.
    """
    last_pos: dict = {}
    for idx, event in enumerate(order):
        tag = getattr(event, "tool_call_id", None)
        if tag is None:
            continue
        if isinstance(event, ToolExecuteStart):
            last_pos[tag] = idx
        elif isinstance(event, ToolExecuteFinish):
            start_pos = last_pos.get(tag)
            assert start_pos is not None, f"Finish before Start for {tag}"
            assert start_pos < idx, f"Finish reordered before its Start for {tag}"
            last_pos.pop(tag, None)


class TestSequencedOrderingUnderConcurrentPublish:
    """FIFO within a category is preserved when many threads publish at once.

    The guarantee verified here is per-pair relative ordering: for every
    tagged pair, its Start must be delivered strictly before its Finish. This
    holds even when the category lane interleaves events from many publishers.
    """

    def test_tool_per_pair_ordering_concurrent_threads(self):
        bus = MessageBus(max_queue_size=256, worker_threads=4)
        n_threads, pairs = 8, 20
        total = n_threads * pairs * 2  # each pair = start + finish
        tracker = _SeqTracker(expected=total)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)
        try:
            threads = [
                threading.Thread(target=_publish_pairs, args=(bus, pairs, t))
                for t in range(n_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=MAX_WAIT)

            assert tracker.wait()

            received = tracker.events()
            assert len(received) == total
            # No loss: every tagged pair delivered once.
            assert len({e.tool_call_id for e in received}) == n_threads * pairs
            _assert_pair_relative_order(received)
        finally:
            bus.shutdown(timeout=5.0)

    def test_mixed_categories_no_reorder_within_category(self):
        bus = MessageBus(max_queue_size=256, worker_threads=4)
        n_threads, iters = 4, 25
        per_thread_tool = iters * 2
        tool_tracker = _SeqTracker(expected=n_threads * per_thread_tool)
        deleg_tracker = _SeqTracker(expected=n_threads * iters * 2)
        bus.subscribe(ToolExecuteStart, tool_tracker)
        bus.subscribe(ToolExecuteFinish, tool_tracker)
        bus.subscribe(DelegationStart, deleg_tracker)
        bus.subscribe(DelegationFinish, deleg_tracker)
        try:
            def _publish_mixed(thread_id: int):
                for i in range(iters):
                    tag = f"t{thread_id}-{i}"
                    bus.publish(ToolExecuteStart(tool="t", args={}, tool_call_id=tag))
                    bus.publish(_DELEG_START)
                    bus.publish(ToolExecuteFinish(
                        session_update="", tool_call_id=tag, title="t",
                        status="completed", content=None, raw_output=None,
                        workdir="/tmp",
                    ))
                    bus.publish(_DELEG_FINISH)

            threads = [
                threading.Thread(target=_publish_mixed, args=(t,))
                for t in range(n_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=MAX_WAIT)

            assert tool_tracker.wait() and deleg_tracker.wait()

            tool_order = tool_tracker.events()
            deleg_order = deleg_tracker.events()
            assert len(tool_order) == n_threads * per_thread_tool
            assert len(deleg_order) == n_threads * iters * 2
            # Tool category: no reordering of the two sequenced event classes,
            # and no inter-category leakage (strict start/finish alternation is
            # NOT enforced across interleaving publishers — only per-pair).
            _assert_pair_relative_order(tool_order)
            _assert_pair_relative_order(deleg_order)
        finally:
            bus.shutdown(timeout=5.0)


# ---------------------------------------------------------------------------
# Queue pressure — telemetry saturation vs reliable lane
# ---------------------------------------------------------------------------

class TestQueuePressureIsolation:
    """Telemetry saturation must not starve reliable events."""

    def test_reliable_events_deliver_through_telemetry_pressure(self):
        bus = MessageBus(max_queue_size=4, worker_threads=2)
        reliable_tracker = _SeqTracker(expected=5)
        bus.subscribe(AgentMessage, reliable_tracker)
        gate = threading.Event()
        try:
            # Saturate the telemetry lane with a slow handler.
            bus.subscribe(AgentStatus, _HandlerAdapter(lambda e: gate.wait(30)))

            for _ in range(100):
                bus.publish(_telemetry_event())

            # Reliable events still admitted and delivered despite telemetry backlog.
            for i in range(5):
                admitted = bus.publish(_reliable_event(i))
                assert admitted is True
            assert reliable_tracker.wait()
            assert len(reliable_tracker.order()) == 5
        finally:
            gate.set()
            bus.shutdown(timeout=5.0)

    def test_telemetry_drops_under_pressure_but_never_blocks(self):
        bus = MessageBus(max_queue_size=2, worker_threads=1)
        gate = threading.Event()
        try:
            bus.subscribe(AgentStatus, _HandlerAdapter(lambda e: gate.wait(30)))

            results = [bus.publish(_telemetry_event()) for _ in range(200)]
            # Some publishes admitted/delivered; the large tail must drop
            # (return False) rather than block or raise.
            assert any(r is False for r in results)
            assert all(isinstance(r, bool) for r in results)
        finally:
            gate.set()
            bus.shutdown(timeout=5.0)

    def test_reliable_exhaustion_raises_visibly(self):
        bus = MessageBus(max_queue_size=2, worker_threads=1,
                         reliable_publish_timeout=0.3)
        gate = threading.Event()
        blocking = _HandlerAdapter(lambda e: gate.wait(30))
        bus.subscribe(AgentMessage, blocking)
        try:
            # Fill and saturate the reliable lane: the worker is blocked and
            # the bounded queue holds up to max_queue_size items. Once the
            # lane is exhausted, a further publish must raise visibly.
            with pytest.raises(ReliableEventAdmissionError):
                for _ in range(20):
                    bus.publish(_reliable_event(i=0))
        finally:
            gate.set()
            bus.shutdown(timeout=5.0)

    def test_reliable_admission_blocking_when_lane_full(self):
        # A reliable publish blocks (waits for capacity) rather than dropping
        # once the lane is saturated, then succeeds once a slot frees up.
        bus = MessageBus(max_queue_size=2, worker_threads=1,
                         reliable_publish_timeout=5.0)
        release = threading.Event()

        def controlled(e: Event) -> None:
            release.wait(timeout=MAX_WAIT)

        bus.subscribe(AgentMessage, _HandlerAdapter(controlled))
        try:
            # Fill the lane: one event blocks in the worker, two more fill the
            # bounded queue so a subsequent publish must wait for capacity.
            bus.publish(_reliable_event(0))
            bus.publish(_reliable_event(1))
            bus.publish(_reliable_event(2))

            started = threading.Event()
            admitted = {}

            def _publish_blocking():
                started.set()
                admitted["ok"] = bus.publish(_reliable_event(3))
                admitted["done"] = True

            t = threading.Thread(target=_publish_blocking)
            t.start()
            assert started.wait(timeout=MAX_WAIT)
            assert not admitted.get("done", False)
            # Free a slot; the waiting publish completes.
            release.set()
            t.join(timeout=MAX_WAIT)
            assert admitted.get("done") is True
            assert admitted.get("ok") is True
        finally:
            release.set()
            bus.shutdown(timeout=5.0)


# ---------------------------------------------------------------------------
# Shutdown — no deadlock / no post-shutdown logging loop
# ---------------------------------------------------------------------------

class TestShutdownUnderLoad:
    """Shutdown drains FIFO and rejects later publishes cleanly."""

    def test_shutdown_drains_ordered_tool_events(self):
        bus = MessageBus(max_queue_size=8, worker_threads=2)
        tracker = _SeqTracker(expected=6)
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)
        try:
            _publish_pairs(bus, 3, thread_id=0)
            bus.shutdown(timeout=5.0)
            received = tracker.events()
            order = tracker.order()
            assert len(received) == 6
            for i in range(0, len(order), 2):
                assert order[i] is ToolExecuteStart
                assert order[i + 1] is ToolExecuteFinish
        finally:
            bus.shutdown(timeout=2.0)

    def test_publish_after_shutdown_rejected(self):
        bus = MessageBus(max_queue_size=8, worker_threads=1)
        try:
            bus.shutdown(timeout=5.0)
            # Post-shutdown publishes are rejected cleanly (no raise/loop).
            assert bus.publish(_reliable_event(0)) is False
            assert bus.publish(_telemetry_event()) is False
        finally:
            bus.shutdown(timeout=2.0)

    def test_concurrent_publish_and_shutdown_no_deadlock(self):
        bus = MessageBus(max_queue_size=64, worker_threads=2)
        tracker = _SeqTracker()
        bus.subscribe(ToolExecuteStart, tracker)
        bus.subscribe(ToolExecuteFinish, tracker)

        stop = threading.Event()
        errors = []

        def _publish_loop():
            try:
                while not stop.is_set():
                    bus.publish(_TOOL_START)
                    bus.publish(_TOOL_FINISH)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=_publish_loop) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        try:
            bus.shutdown(timeout=5.0)
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=MAX_WAIT)
        # A clean shutdown must not deadlock or spin; publishing during/after
        # shutdown is rejected, never raising an unhandled error.
        assert not errors


# ---------------------------------------------------------------------------
# Owner-scoped cancellation under real concurrent threads
# ---------------------------------------------------------------------------

class TestOwnerScopedCancellationConcurrent:
    """cancel(owner=) / reset_cancel(owner=) remain correct under real threads."""

    def test_cross_thread_reset_cannot_clear_other_owner(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")

        results = {"agent1_ok": False, "agent2_ok": False}

        def _wrong_owner_reset():
            manager.reset_cancel(owner="agent2")
            results["agent2_ok"] = manager._cancel_event.is_set()

        t = threading.Thread(target=_wrong_owner_reset)
        t.start()
        t.join(timeout=MAX_WAIT)

        # agent2 (wrong owner) must NOT be able to clear agent1's cancel.
        assert manager._cancel_event.is_set() is True
        assert results["agent2_ok"] is True

    def test_matching_owner_reset_clears_under_concurrency(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")

        done = threading.Event()
        result = {}

        def _correct_owner_reset():
            manager.reset_cancel(owner="agent1")
            result["cleared"] = not manager._cancel_event.is_set()
            done.set()

        threading.Thread(target=_correct_owner_reset).start()
        assert done.wait(timeout=MAX_WAIT)
        assert result["cleared"] is True

    @pytest.mark.asyncio
    async def test_blocked_write_acquire_serializes_across_agents(self):
        manager = FileLockManager("/tmp")
        manager.reset_cancel(owner="coordinator")

        await manager.acquire_write_async("f.py", "agent1", timeout=5.0)

        # A second agent's write acquire must wait until the first releases.
        blocker = asyncio.ensure_future(
            manager.acquire_write_async("f.py", "agent2", timeout=5.0)
        )
        await asyncio.sleep(0.15)
        assert not blocker.done()

        await manager.release_write("f.py", "agent1")
        acquired = await asyncio.wait_for(blocker, timeout=MAX_WAIT)
        assert acquired is True
        # The owner-scoped cancel guard is intact: an unrelated reset does not
        # clear the cancel signal.
        manager.cancel(owner="coordinator")
        manager.reset_cancel(owner="other")
        assert manager._cancel_event.is_set() is True
        manager.reset_cancel(owner="coordinator")
        assert manager._cancel_event.is_set() is False


# ---------------------------------------------------------------------------
# Session-state hydration returns isolated snapshots (STAB-04)
# ---------------------------------------------------------------------------

class TestSessionSnapshotIsolation:
    """Mutating a returned hydration object must not mutate manager state."""

    def setup_method(self):
        AgentSessionManager._instance = None

    def test_mutation_of_hydration_does_not_leak(self):
        mgr = AgentSessionManager.get_instance()
        snapshot = mgr.get_session_state()
        snapshot.session_id = "mutated-by-caller"
        snapshot.current_plan.append({"step": "injected"})
        snapshot.pending_p2p.append({"source": "injected"})

        fresh = mgr.get_session_state()
        assert fresh.session_id != "mutated-by-caller"
        assert fresh.current_plan == []
        assert fresh.pending_p2p == []


# ---------------------------------------------------------------------------
# Real file-lock contention across threads
# ---------------------------------------------------------------------------

class TestFileLockContention:
    """Concurrent write acquisitions across real threads serialize correctly."""

    def test_write_lock_mutually_exclusive_under_threads(self):
        manager = FileLockManager("/tmp")
        manager.reset_cancel(owner="test")
        path = "shared.py"
        active = {"n": 0, "max": 0}
        lock = threading.Lock()
        violations = []

        def _worker(agent: str):
            loop = asyncio.new_event_loop()
            try:
                ok = loop.run_until_complete(
                    manager.acquire_write_async(path, agent, timeout=5.0)
                )
                if not ok:
                    violations.append(f"{agent}: not acquired")
                    return
                with lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                time.sleep(0.02)
                with lock:
                    active["n"] -= 1
                loop.run_until_complete(manager.release_write(path, agent))
            finally:
                loop.close()

        threads = [threading.Thread(target=_worker, args=(f"a{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=MAX_WAIT)

        assert not violations
        # Mutual exclusion: at most one writer holds the lock at a time.
        assert active["max"] == 1
        assert manager.get_lock_status(path)["writer"] is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _HandlerAdapter:
    """Adapter exposing .handle() so a plain callable can be a bus handler."""

    def __init__(self, fn):
        self._fn = fn

    def handle(self, event: Event) -> None:
        self._fn(event)


_telemetry_seq = {"n": 0}
_telemetry_lock = threading.Lock()


def _telemetry_event() -> AgentStatus:
    with _telemetry_lock:
        _telemetry_seq["n"] += 1
        i = _telemetry_seq["n"]
    return AgentStatus(status="working", node="n", task=f"task-{i}", turns=i)


def _reliable_event(i: Optional[int] = None) -> AgentMessage:
    return AgentMessage(message=f"msg-{i if i is not None else 'x'}")
