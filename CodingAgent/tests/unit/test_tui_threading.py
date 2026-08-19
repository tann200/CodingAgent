"""
Threading safety tests for AgentBridge (migrated from src.ui — LEGACY-02).

Originally verified TextualAppBase threading correctness. Rewritten against
AgentBridge which carries identical threading contracts:
  _agent_lock, _cancel_event, _history_lock, send_prompt(), history.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge():
    """Build an AgentBridge with a mocked orchestrator (no real LLM calls)."""
    from tui.tui_src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
    from tui.tui_src.ui.core_bridge import AgentBridge

    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.app = mock_app
    bridge._bus = bus
    bridge._working_dir = ""
    bridge._active_role = "operational"
    bridge._agent_lock = threading.Lock()
    bridge._agent_running = False
    bridge._history_lock = threading.Lock()
    bridge.history = []
    bridge._cancel_event = threading.Event()
    bridge._subscriptions = []
    bridge._pending_injections = []  # MID-INJ: required by send_prompt

    # Stub orchestrator so no real LLM calls happen
    orch = MagicMock()
    orch.start_new_task.return_value = "task-001"
    orch.flush_execution_trace = MagicMock()
    # run_agent_once is async — return a coroutine-like result

    async def _fake_run(*a, **kw):
        return {"response": "ok", "last_result": {"output": "ok"}}

    orch.run_agent_once.side_effect = _fake_run
    orch.get_tools_for_role.return_value = []
    bridge._orchestrator = orch

    # Suppress session snapshot on run finish
    mock_app._save_session_snapshot = MagicMock()

    # Suppress role-config lookup (no file system dependency)
    bridge._active_role = "full_stack_engineer"

    return bridge


# ---------------------------------------------------------------------------
# History-lock tests
# ---------------------------------------------------------------------------


class TestHistoryLock:
    def test_history_lock_exists(self):
        """AgentBridge must expose a threading.Lock for history protection."""
        bridge = _make_bridge()
        assert hasattr(bridge, "_history_lock")
        assert isinstance(bridge._history_lock, type(threading.Lock()))

    def test_send_prompt_appends_user_message(self):
        """send_prompt must append the user message before launching the thread."""
        bridge = _make_bridge()
        bridge.send_prompt("hello world")
        # Wait briefly for the background thread to start
        time.sleep(0.1)
        assert any(
            role == "user" and "hello world" in text for role, text in bridge.history
        )

    def test_send_prompt_returns_false_when_running(self):
        """send_prompt returns False immediately if agent is already running."""
        bridge = _make_bridge()
        with bridge._agent_lock:
            bridge._agent_running = True
        result = bridge.send_prompt("blocked")
        assert result is False

    def test_sequential_prompts_preserve_user_messages(self):
        """User messages from sequential send_prompt calls appear in history."""
        bridge = _make_bridge()

        bridge.send_prompt("msg0")
        time.sleep(0.15)

        # Wait for first agent to complete (simulate by directly resetting flag)
        # In real usage, _run_agent's finally block does this after completion
        bridge._agent_running = False

        bridge.send_prompt("msg1")
        time.sleep(0.15)

        user_msgs = [text for role, text in bridge.history if role == "user"]
        assert "msg0" in user_msgs, f"msg0 not in {user_msgs}"
        assert "msg1" in user_msgs, f"msg1 not in {user_msgs}"


# ---------------------------------------------------------------------------
# Concurrent access tests
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_concurrent_history_reads_no_exception(self):
        """Reading history while the agent thread writes must not raise."""
        bridge = _make_bridge()
        errors: list[Exception] = []

        def read_loop():
            for _ in range(50):
                try:
                    _ = list(bridge.history)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        bridge.send_prompt("concurrent test")
        reader = threading.Thread(target=read_loop, daemon=True)
        reader.start()
        reader.join(timeout=1)
        assert errors == [], f"Concurrent read raised: {errors}"

    def test_lock_acquired_without_deadlock(self):
        """Lock can be acquired without deadlock when held briefly."""
        bridge = _make_bridge()
        start = time.time()
        with bridge._history_lock:
            bridge.history.append(("system", "test1"))
            time.sleep(0.01)
            bridge.history.append(("system", "test2"))
        elapsed = time.time() - start
        assert elapsed < 1.0, "Lock acquisition took unexpectedly long"
        texts = [text for _, text in bridge.history if text.startswith("test")]
        assert "test1" in texts and "test2" in texts

    def test_history_entries_are_tuples(self):
        """All history entries must be 2-tuples of (role, text)."""
        bridge = _make_bridge()
        bridge.send_prompt("t0")
        time.sleep(0.1)
        for entry in bridge.history:
            assert isinstance(entry, tuple), f"History entry is not a tuple: {entry!r}"
            assert len(entry) == 2, f"History entry wrong length: {entry!r}"
            role, text = entry
            assert isinstance(role, str)
            assert isinstance(text, str)
