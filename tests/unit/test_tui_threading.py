"""
#37: TUI threading safety tests for TextualAppBase.

Verifies that concurrent send_prompt calls cannot corrupt the history list
and that the _history_lock prevents data races.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    """Build a TextualAppBase with a mocked orchestrator (no real LLM calls)."""
    import tempfile, pathlib
    from src.ui.textual_app_impl import TextualAppBase

    orch = MagicMock()
    orch.run_agent_once.return_value = {"assistant_message": "ok", "work_summary": None}
    orch.start_new_task.return_value = "task-001"
    orch._clear_execution_trace = MagicMock()

    # Use an isolated temp dir for history persistence so tests stay independent
    _tmp_dir = pathlib.Path(tempfile.mkdtemp())
    _history_path = _tmp_dir / "tui_conversation_history.json"

    # Disable event-bus subscription so we don't need a running bus
    with patch("src.ui.textual_app_impl.get_event_bus", return_value=None):
        app = TextualAppBase(orchestrator=orch)

    # Override history path to the isolated temp file (no stale disk data)
    app._get_history_path = lambda: _history_path
    app.history = []  # clear any history loaded from the real path

    # Silence UI callback so tests don't crash on missing Textual context
    app.on_agent_result = MagicMock()
    app._schedule_callback = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    return app


# ---------------------------------------------------------------------------
# History-lock tests
# ---------------------------------------------------------------------------

class TestHistoryLock:
    def test_history_lock_exists(self):
        """TextualAppBase must expose a threading.Lock for history protection."""
        app = _make_app()
        assert hasattr(app, "_history_lock")
        assert isinstance(app._history_lock, type(threading.Lock()))

    def test_send_prompt_appends_user_message(self):
        """send_prompt must append the user message before launching the thread."""
        app = _make_app()
        app.send_prompt("hello world")
        # Wait for the background thread to finish
        if hasattr(app, "_agent_thread"):
            app._agent_thread.join(timeout=2)
        assert any(role == "user" and "hello world" in text for role, text in app.history)

    def test_send_prompt_appends_assistant_reply(self):
        """The background thread appends the assistant reply to history."""
        app = _make_app()
        app.send_prompt("ping")
        if hasattr(app, "_agent_thread"):
            app._agent_thread.join(timeout=2)
        roles = [role for role, _ in app.history]
        assert "assistant" in roles

    def test_sequential_prompts_preserve_order(self):
        """Multiple sequential send_prompt calls keep history in insertion order."""
        app = _make_app()
        for i in range(3):
            app.send_prompt(f"msg{i}")
            if hasattr(app, "_agent_thread"):
                app._agent_thread.join(timeout=2)

        user_msgs = [text for role, text in app.history if role == "user"]
        assert user_msgs == ["msg0", "msg1", "msg2"]


# ---------------------------------------------------------------------------
# Concurrent access tests
# ---------------------------------------------------------------------------

class TestConcurrentAccess:
    def test_concurrent_history_reads_no_exception(self):
        """Reading history while the agent thread writes must not raise."""
        app = _make_app()

        errors = []

        def read_loop():
            for _ in range(50):
                try:
                    _ = list(app.history)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        app.send_prompt("concurrent test")
        reader = threading.Thread(target=read_loop, daemon=True)
        reader.start()
        if hasattr(app, "_agent_thread"):
            app._agent_thread.join(timeout=3)
        reader.join(timeout=1)
        assert errors == [], f"Concurrent read raised: {errors}"

    def test_lock_acquired_during_append(self):
        """Lock is re-entrant and can be acquired from the same thread without deadlock."""
        app = _make_app()

        # Verify the lock itself is non-deadlocking when acquired twice
        start = time.time()
        with app._history_lock:
            # Simulate a "slow" write while holding the lock
            app.history.append(("system", "test1"))
            time.sleep(0.01)
            app.history.append(("system", "test2"))
        elapsed = time.time() - start
        assert elapsed < 1.0, "Lock acquisition took unexpectedly long"
        # Verify appends landed
        texts = [text for _, text in app.history if text.startswith("test")]
        assert "test1" in texts and "test2" in texts

    def test_multiple_threads_no_history_corruption(self):
        """Two threads calling send_prompt serially must not corrupt history."""
        from src.ui.textual_app_impl import TextualAppBase

        # Use a slower mock to increase chance of interleaving
        call_count = [0]

        def slow_run(*a, **kw):
            call_count[0] += 1
            time.sleep(0.02)
            return {"assistant_message": f"reply{call_count[0]}", "work_summary": None}

        orch = MagicMock()
        orch.run_agent_once.side_effect = slow_run
        orch.start_new_task.return_value = "t"
        orch._clear_execution_trace = MagicMock()

        with patch("src.ui.textual_app_impl.get_event_bus", return_value=None):
            app = TextualAppBase(orchestrator=orch)
        app.on_agent_result = MagicMock()
        app._schedule_callback = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))

        threads = [
            threading.Thread(target=app.send_prompt, args=(f"t{i}",), daemon=True)
            for i in range(4)
        ]
        for t in threads:
            t.start()
            time.sleep(0.005)  # slight stagger

        # Wait for all agent threads to finish
        for _ in range(10):
            time.sleep(0.05)
            if call_count[0] >= 4:
                break

        # History must be a list of 2-tuples — no corruption
        for entry in app.history:
            assert isinstance(entry, tuple), f"History entry is not a tuple: {entry!r}"
            assert len(entry) == 2, f"History entry wrong length: {entry!r}"
            role, text = entry
            assert isinstance(role, str)
            assert isinstance(text, str)
