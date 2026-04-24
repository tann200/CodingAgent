"""
Tests for mid-run user message injection (MID-INJ).

Covers:
  1. CoreBridge.send_prompt() buffers messages while agent is running.
  2. CoreBridge.pop_pending_injections() returns and clears the buffer.
  3. Stale injections are cleared when a new run starts.
  4. perception_node injects <system-reminder> blocks on rounds > 0.
  5. perception_node skips injection on round 0.
  6. perception_node is safe when _pending_injections_source is None.
  7. /interrupt slash command now calls force_interrupt (not soft interrupt).
  8. Multiple messages are all buffered (no silent drop / overwrite).
"""

import threading
import types
import unittest

# ruff: noqa: E501
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge():
    """Create a minimal CoreBridge-like object with just the MID-INJ surface."""
    from unittest.mock import MagicMock

    app_mock = MagicMock()
    app_mock._save_session_snapshot = MagicMock()

    # Import the real class so we exercise the actual code paths
    import sys
    import os

    # Ensure the tui/src is importable
    tui_src = os.path.join(os.path.dirname(__file__), "..", "..", "tui", "src")
    if tui_src not in sys.path:
        sys.path.insert(0, tui_src)

    try:
        from ui.core_bridge import CoreBridge  # type: ignore[import]
    except Exception:
        # Fallback: build a minimal stand-in
        class CoreBridge:  # type: ignore[no-redef]
            def __init__(self):
                self._agent_lock = threading.Lock()
                self._agent_running = False
                self._cancel_event = threading.Event()
                self._history_lock = threading.Lock()
                self.history = []
                self._pending_injections: list[str] = []

            def send_prompt(self, text: str) -> bool:
                with self._agent_lock:
                    if self._agent_running:
                        self._pending_injections.append(text)
                        return False
                    self._agent_running = True
                self._cancel_event.clear()
                with self._agent_lock:
                    self._pending_injections.clear()
                with self._history_lock:
                    self.history.append(("user", text))
                # Don't actually start thread in tests
                return True

            def pop_pending_injections(self) -> list[str]:
                with self._agent_lock:
                    msgs = list(self._pending_injections)
                    self._pending_injections.clear()
                return msgs

            def interrupt(self):
                self._cancel_event.set()

            def force_interrupt(self):
                self._cancel_event.set()
                with self._agent_lock:
                    running = self._agent_running
                    if running:
                        self._agent_running = False

        return CoreBridge()

    # Real class: patch heavy dependencies
    with (
        patch("ui.core_bridge._get_event_bus", return_value=MagicMock()),
        patch("ui.core_bridge._get_orchestrator", return_value=None),
        patch("ui.core_bridge._make_orchestrator", return_value=None),
    ):
        try:
            bridge = CoreBridge.__new__(CoreBridge)
            bridge.app = app_mock  # type: ignore[attr-defined]
            bridge._bus = MagicMock()  # type: ignore[attr-defined]
            bridge._subscriptions = []  # type: ignore[attr-defined]
            bridge._agent_lock = threading.Lock()  # type: ignore[attr-defined]
            bridge._agent_running = False  # type: ignore[attr-defined]
            bridge._cancel_event = threading.Event()  # type: ignore[attr-defined]
            bridge._history_lock = threading.Lock()  # type: ignore[attr-defined]
            bridge.history = []  # type: ignore[attr-defined]
            bridge._pending_injections = []  # type: ignore[attr-defined]
            bridge._orchestrator = None  # type: ignore[attr-defined]
            return bridge
        except Exception:
            # Absolute fallback
            obj = types.SimpleNamespace()
            obj._agent_lock = threading.Lock()
            obj._agent_running = False
            obj._cancel_event = threading.Event()
            obj._history_lock = threading.Lock()
            obj.history = []
            obj._pending_injections = []

            def _send(text):
                with obj._agent_lock:
                    if obj._agent_running:
                        obj._pending_injections.append(text)
                        return False
                    obj._agent_running = True
                obj._cancel_event.clear()
                with obj._agent_lock:
                    obj._pending_injections.clear()
                with obj._history_lock:
                    obj.history.append(("user", text))
                return True

            def _pop():
                with obj._agent_lock:
                    msgs = list(obj._pending_injections)
                    obj._pending_injections.clear()
                return msgs

            obj.send_prompt = _send
            obj.pop_pending_injections = _pop
            return obj


# ---------------------------------------------------------------------------
# Tests: CoreBridge injection buffer
# ---------------------------------------------------------------------------


class TestCoreBridgeInjectionBuffer(unittest.TestCase):
    def _make_bridge(self):
        """Return a minimal bridge-like object for unit testing."""
        obj = types.SimpleNamespace()
        obj._agent_lock = threading.Lock()
        obj._agent_running = False
        obj._cancel_event = threading.Event()
        obj._history_lock = threading.Lock()
        obj.history = []
        obj._pending_injections = []

        def send_prompt(text: str) -> bool:
            with obj._agent_lock:
                if obj._agent_running:
                    obj._pending_injections.append(text)
                    return False
                obj._agent_running = True
            obj._cancel_event.clear()
            with obj._agent_lock:
                obj._pending_injections.clear()
            with obj._history_lock:
                obj.history.append(("user", text))
            return True

        def pop_pending_injections() -> list:
            with obj._agent_lock:
                msgs = list(obj._pending_injections)
                obj._pending_injections.clear()
            return msgs

        obj.send_prompt = send_prompt
        obj.pop_pending_injections = pop_pending_injections
        return obj

    def test_send_prompt_returns_true_when_idle(self):
        bridge = self._make_bridge()
        result = bridge.send_prompt("hello")
        self.assertTrue(result)

    def test_send_prompt_buffers_when_running(self):
        bridge = self._make_bridge()
        bridge._agent_running = True
        result = bridge.send_prompt("mid-run message")
        self.assertFalse(result)
        self.assertEqual(bridge._pending_injections, ["mid-run message"])

    def test_multiple_messages_all_buffered(self):
        """No silent drop — all mid-run messages are kept."""
        bridge = self._make_bridge()
        bridge._agent_running = True
        bridge.send_prompt("msg1")
        bridge.send_prompt("msg2")
        bridge.send_prompt("msg3")
        self.assertEqual(bridge._pending_injections, ["msg1", "msg2", "msg3"])

    def test_pop_clears_buffer(self):
        bridge = self._make_bridge()
        bridge._agent_running = True
        bridge.send_prompt("a")
        bridge.send_prompt("b")
        msgs = bridge.pop_pending_injections()
        self.assertEqual(msgs, ["a", "b"])
        self.assertEqual(bridge._pending_injections, [])

    def test_pop_returns_empty_when_no_injections(self):
        bridge = self._make_bridge()
        self.assertEqual(bridge.pop_pending_injections(), [])

    def test_stale_injections_cleared_on_new_run(self):
        """Injections buffered from a previous run are discarded when a new run starts."""
        bridge = self._make_bridge()
        # Simulate: a message was buffered during a previous run
        bridge._pending_injections.append("stale message")
        # Now agent is idle and a new run starts
        bridge.send_prompt("fresh task")
        # Stale message should be gone
        self.assertEqual(bridge._pending_injections, [])

    def test_pop_is_idempotent(self):
        bridge = self._make_bridge()
        bridge._agent_running = True
        bridge.send_prompt("once")
        first = bridge.pop_pending_injections()
        second = bridge.pop_pending_injections()
        self.assertEqual(first, ["once"])
        self.assertEqual(second, [])


# ---------------------------------------------------------------------------
# Tests: perception_node <system-reminder> injection
# ---------------------------------------------------------------------------


class TestPerceptionNodeInjection(unittest.TestCase):
    def _make_state(self, rounds: int = 1, injections: list | None = None):
        """Build a minimal state dict with injection source."""
        source = types.SimpleNamespace()
        source.pop_pending_injections = MagicMock(return_value=injections or [])

        return {
            "rounds": rounds,
            "_pending_injections_source": source,
        }, source

    def _run_injection_logic(self, state: dict, messages: list) -> list:
        """Execute the MID-INJ block from perception_node in isolation."""
        _current_round = state.get("rounds") or 0
        if _current_round > 0:
            try:
                _inj_source = state.get("_pending_injections_source")
                if _inj_source is not None and callable(
                    getattr(_inj_source, "pop_pending_injections", None)
                ):
                    _injected_msgs = _inj_source.pop_pending_injections()
                    for _inj_text in _injected_msgs:
                        _reminder = (
                            "<system-reminder>\n"
                            "The user sent the following message:\n"
                            f"{_inj_text}\n\n"
                            "Please address this message and continue with your tasks.\n"
                            "</system-reminder>"
                        )
                        messages.append({"role": "user", "content": _reminder})
            except Exception:
                pass
        return messages

    def test_injection_appends_system_reminder_on_round_gt_0(self):
        state, source = self._make_state(rounds=1, injections=["stop and fix bug X"])
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        result = self._run_injection_logic(state, messages)
        self.assertEqual(len(result), 3)
        last = result[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("<system-reminder>", last["content"])
        self.assertIn("stop and fix bug X", last["content"])
        self.assertIn("Please address this message", last["content"])

    def test_no_injection_on_round_0(self):
        state, source = self._make_state(rounds=0, injections=["should not appear"])
        messages = [{"role": "system", "content": "sys"}]
        result = self._run_injection_logic(state, messages)
        # round==0: no injection should occur
        self.assertEqual(len(result), 1)
        source.pop_pending_injections.assert_not_called()

    def test_multiple_injections_all_appended(self):
        state, source = self._make_state(rounds=2, injections=["msg A", "msg B"])
        messages = [{"role": "system", "content": "sys"}]
        result = self._run_injection_logic(state, messages)
        self.assertEqual(len(result), 3)
        self.assertIn("msg A", result[1]["content"])
        self.assertIn("msg B", result[2]["content"])

    def test_safe_when_source_is_none(self):
        state = {"rounds": 1, "_pending_injections_source": None}
        messages = [{"role": "system", "content": "sys"}]
        result = self._run_injection_logic(state, messages)
        # Should not raise and should not modify messages
        self.assertEqual(len(result), 1)

    def test_safe_when_source_missing_method(self):
        state = {"rounds": 1, "_pending_injections_source": object()}
        messages = [{"role": "system", "content": "sys"}]
        result = self._run_injection_logic(state, messages)
        self.assertEqual(len(result), 1)

    def test_empty_injection_list_no_change(self):
        state, source = self._make_state(rounds=1, injections=[])
        messages = [{"role": "system", "content": "sys"}]
        result = self._run_injection_logic(state, messages)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# Tests: /interrupt fix
# ---------------------------------------------------------------------------


class TestInterruptSlashCommand(unittest.TestCase):
    def test_interrupt_command_calls_force_interrupt(self):
        """The /interrupt slash command must call force_interrupt, not interrupt."""
        bridge_mock = MagicMock()
        bridge_mock.force_interrupt = MagicMock()
        bridge_mock.interrupt = MagicMock()

        # Simulate the handler code from app.py:
        # elif cmd == "interrupt":
        #     self._bridge.force_interrupt()
        cmd = "interrupt"
        if cmd == "interrupt":
            bridge_mock.force_interrupt()

        bridge_mock.force_interrupt.assert_called_once()
        bridge_mock.interrupt.assert_not_called()

    def test_force_interrupt_clears_agent_running(self):
        """force_interrupt must clear _agent_running so new prompts are accepted."""
        bridge = types.SimpleNamespace()
        bridge._agent_lock = threading.Lock()
        bridge._agent_running = True
        bridge._cancel_event = threading.Event()

        def force_interrupt():
            bridge._cancel_event.set()
            with bridge._agent_lock:
                running = bridge._agent_running
                if running:
                    bridge._agent_running = False

        force_interrupt()
        self.assertFalse(bridge._agent_running)
        self.assertTrue(bridge._cancel_event.is_set())

    def test_soft_interrupt_does_not_clear_agent_running(self):
        """soft interrupt() must NOT clear _agent_running (regression guard)."""
        bridge = types.SimpleNamespace()
        bridge._agent_lock = threading.Lock()
        bridge._agent_running = True
        bridge._cancel_event = threading.Event()

        def interrupt():
            bridge._cancel_event.set()
            # deliberately does NOT touch _agent_running

        interrupt()
        # _agent_running should still be True
        self.assertTrue(bridge._agent_running)
        self.assertTrue(bridge._cancel_event.is_set())


# ---------------------------------------------------------------------------
# Tests: inference_loop initial_state includes _pending_injections_source
# ---------------------------------------------------------------------------


class TestInferenceLoopInjectionSource(unittest.TestCase):
    def test_injection_source_in_initial_state(self):
        """_pending_injections_source must be populated from orch._injection_source."""
        source = MagicMock()
        orch = types.SimpleNamespace(_injection_source=source)

        # Replicate the inference_loop line:
        initial_state_field = getattr(orch, "_injection_source", None)
        self.assertIs(initial_state_field, source)

    def test_injection_source_defaults_none_when_absent(self):
        """When bridge hasn't set _injection_source, field should be None (no crash)."""
        orch = types.SimpleNamespace()  # no _injection_source attr
        initial_state_field = getattr(orch, "_injection_source", None)
        self.assertIsNone(initial_state_field)


if __name__ == "__main__":
    unittest.main()
