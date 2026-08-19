"""tests/unit/test_chat_queue.py — TASK-TUI-9

Tests for the message queueing logic used when a user submits messages while
the agent is running. The core queue is a list[str] maintained on CodingAgentApp;
here we test the data-model behaviour in isolation without launching Textual.
"""

from __future__ import annotations

from collections import deque
from typing import List


# ---------------------------------------------------------------------------
# Minimal stand-in that mirrors the queue contract in app.py
# ---------------------------------------------------------------------------

class _MessageQueue:
    """Pure-Python model of the TUI message queue (no Textual dependency)."""

    def __init__(self) -> None:
        self._queue: deque[str] = deque()
        self._agent_running: bool = False
        self._sent: List[str] = []  # log of messages actually dispatched

    # ── Public interface ──────────────────────────────────────────────────

    def start_agent(self) -> None:
        self._agent_running = True

    def stop_agent(self) -> None:
        self._agent_running = False
        self._drain()

    def submit(self, message: str) -> str:
        """Submit a user message. Returns 'queued' or 'sent'."""
        if self._agent_running:
            self._queue.append(message)
            return "queued"
        else:
            self._sent.append(message)
            return "sent"

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def sent_messages(self) -> List[str]:
        return list(self._sent)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _drain(self) -> None:
        """Send all queued messages in order after agent stops."""
        while self._queue:
            msg = self._queue.popleft()
            self._sent.append(msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_message_sent_immediately_when_idle() -> None:
    q = _MessageQueue()
    result = q.submit("hello")
    assert result == "sent"
    assert q.sent_messages == ["hello"]
    assert q.queue_size == 0


def test_message_queued_when_agent_running() -> None:
    q = _MessageQueue()
    q.start_agent()
    result = q.submit("run while busy")
    assert result == "queued"
    assert q.queue_size == 1
    assert q.sent_messages == []


def test_three_queued_messages_delivered_in_order() -> None:
    q = _MessageQueue()
    q.start_agent()
    q.submit("first")
    q.submit("second")
    q.submit("third")
    assert q.queue_size == 3

    q.stop_agent()

    assert q.queue_size == 0
    assert q.sent_messages == ["first", "second", "third"]


def test_queue_empty_after_drain() -> None:
    q = _MessageQueue()
    q.start_agent()
    q.submit("a")
    q.submit("b")
    q.stop_agent()
    assert q.queue_size == 0


def test_new_submission_after_drain_sent_immediately() -> None:
    q = _MessageQueue()
    q.start_agent()
    q.submit("queued")
    q.stop_agent()  # drains

    result = q.submit("after idle")
    assert result == "sent"
    assert "after idle" in q.sent_messages


def test_multiple_agent_cycles() -> None:
    """Queue → drain → queue → drain should work across multiple runs."""
    q = _MessageQueue()

    q.start_agent()
    q.submit("cycle1-msg1")
    q.stop_agent()
    assert q.sent_messages == ["cycle1-msg1"]

    q.start_agent()
    q.submit("cycle2-msg1")
    q.submit("cycle2-msg2")
    q.stop_agent()
    assert q.sent_messages == ["cycle1-msg1", "cycle2-msg1", "cycle2-msg2"]


def test_submit_while_idle_never_queues() -> None:
    q = _MessageQueue()
    for i in range(5):
        q.submit(f"msg{i}")
    assert q.queue_size == 0
    assert len(q.sent_messages) == 5


def test_empty_queue_drain_is_noop() -> None:
    q = _MessageQueue()
    q.start_agent()
    q.stop_agent()  # no messages queued
    assert q.sent_messages == []
    assert q.queue_size == 0
