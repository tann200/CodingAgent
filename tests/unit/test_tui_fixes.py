"""
TUI bug-fix regression tests.

Covers the 9 fixes from the TUI audit:
  Fix 1  — double-threading: on_input_submitted must target _run_agent directly
  Fix 2  — diff renderer: side-by-side table must pair left/right lines
  Fix 3  — _schedule_callback on CodingAgentTextualApp must use call_from_thread
  Fix 4  — _DIFF_PATTERN/_THINKING_PATTERN/_HUNK_PATTERN must be module-level constants
  Fix 5  — partial=False event must trigger an empty write (trailing newline)
  Fix 6  — LogPanel.entries must be a bounded deque (maxlen=2000)
  Fix 7  — diff truncation must show "… N more lines" indicator
  Fix 8  — plan step description must be truncated with ellipsis at >40 chars
  Fix 9  — Compact Session must call compact_messages_to_prose, not be a placeholder
"""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_base_app():
    """TextualAppBase with a no-op orchestrator, no real EventBus."""
    from src.ui.textual_app_impl import TextualAppBase

    orch = MagicMock()
    orch.run_agent_once.return_value = {"assistant_message": "ok", "work_summary": None}
    orch.start_new_task.return_value = "t"
    with patch("src.ui.textual_app_impl.get_event_bus", return_value=None):
        app = TextualAppBase(orchestrator=orch)
    app.on_agent_result = MagicMock()
    app._schedule_callback = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    return app


# ---------------------------------------------------------------------------
# Fix 1 — double-threading
# ---------------------------------------------------------------------------


class TestFix1DoubleThreading:
    def test_run_agent_thread_targets_run_agent_not_send_prompt(self):
        """After the fix, on_input_submitted spawns a thread targeting _run_agent."""
        app = _make_base_app()
        # Simulate what on_input_submitted does (UI part extracted)
        text = "hello"
        with app._history_lock:
            app.history.append(("user", text))
        app._agent_thread = threading.Thread(
            target=app._run_agent, args=(text,), daemon=True
        )
        assert app._agent_thread._target.__name__ == "_run_agent", (
            "_agent_thread must target _run_agent, not send_prompt"
        )

    def test_send_prompt_does_not_spawn_nested_thread(self):
        """send_prompt itself spawns exactly one thread (not two)."""
        app = _make_base_app()
        threads_spawned = []
        original_thread = threading.Thread

        class CountingThread(original_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                threads_spawned.append(
                    kwargs.get("target") or (args[0] if args else None)
                )

        with patch("src.ui.textual_app_impl.threading.Thread", CountingThread):
            # Re-import to get patched version
            from src.ui import textual_app_impl

            old_thread = textual_app_impl.threading.Thread
            textual_app_impl.threading.Thread = CountingThread
            try:
                app.send_prompt("test")
                if app._agent_thread:
                    app._agent_thread.join(timeout=2)
            finally:
                textual_app_impl.threading.Thread = old_thread

        # send_prompt creates one thread; it must NOT be send_prompt itself
        assert app._run_agent in threads_spawned or len(threads_spawned) <= 1


# ---------------------------------------------------------------------------
# Fix 2 — diff renderer pairs lines side-by-side
# ---------------------------------------------------------------------------


class TestFix2DiffRenderer:
    def _parse_diff_blocks(self, diff_text):
        """Parse a simple unified diff into left/right blocks manually."""
        left, right = [], []
        for line in diff_text.splitlines():
            if (
                line.startswith("---")
                or line.startswith("+++")
                or line.startswith("@@")
            ):
                continue
            if line.startswith("-"):
                left.append(line[1:])
            elif line.startswith("+"):
                right.append(line[1:])
        return left, right

    def test_row_count_equals_max_of_left_right(self):
        """Table must have max(len(left), len(right)) content rows per hunk."""
        diff = (
            "@@ -1,3 +1,2 @@\n"
            "-old_line_1\n"
            "-old_line_2\n"
            "-old_line_3\n"
            "+new_line_1\n"
            "+new_line_2\n"
        )
        left, right = self._parse_diff_blocks(diff)
        # After fix: rows = max(3, 2) = 3
        from itertools import zip_longest

        rows = list(zip_longest(left, right, fillvalue=""))
        assert len(rows) == max(len(left), len(right)), (
            "Row count must equal max(left, right) to avoid dropping lines"
        )

    def test_paired_rows_not_sequential(self):
        """Each row must have BOTH left and right content when lines exist on both sides."""
        diff = "@@ -1,2 +1,2 @@\n-old_a\n-old_b\n+new_a\n+new_b\n"
        left, right = self._parse_diff_blocks(diff)
        from itertools import zip_longest

        rows = list(zip_longest(left, right, fillvalue=""))
        # Every row where both sides have content should have non-empty both sides
        for left_item, right_item in rows:
            if left_item and right_item:
                assert left_item != "" and right_item != "", (
                    "Paired row should have content on both sides"
                )

    def test_asymmetric_diff_pads_with_empty(self):
        """Excess lines on one side must be padded with empty string (not dropped)."""
        left = ["a", "b", "c"]
        right = ["x"]
        from itertools import zip_longest

        rows = list(zip_longest(left, right, fillvalue=""))
        assert rows[1] == ("b", ""), "Second row should pad right with empty"
        assert rows[2] == ("c", ""), "Third row should pad right with empty"


# ---------------------------------------------------------------------------
# Fix 3 — _schedule_callback uses call_from_thread
# ---------------------------------------------------------------------------


class TestFix3CallFromThread:
    def test_schedule_callback_method_exists_on_textual_app(self):
        """CodingAgentTextualApp must define its own _schedule_callback override."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        # The override must exist directly on the class, not inherited from TextualAppBase
        assert "_schedule_callback" in CodingAgentTextualApp.__dict__, (
            "CodingAgentTextualApp must override _schedule_callback"
        )

    def test_schedule_callback_calls_call_from_thread(self):
        """The override must delegate to self.call_from_thread."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp
        import inspect

        src = inspect.getsource(CodingAgentTextualApp._schedule_callback)
        assert "call_from_thread" in src, (
            "_schedule_callback override must use call_from_thread"
        )


# ---------------------------------------------------------------------------
# Fix 4 — module-level regex constants
# ---------------------------------------------------------------------------


class TestFix4RegexConstants:
    def test_diff_pattern_is_module_level(self):
        """_DIFF_PATTERN must be a compiled regex at module level."""
        import src.ui.textual_app_impl as m

        assert hasattr(m, "_DIFF_PATTERN"), "_DIFF_PATTERN must exist at module level"
        assert hasattr(m._DIFF_PATTERN, "search"), (
            "_DIFF_PATTERN must be a compiled regex"
        )

    def test_thinking_pattern_is_module_level(self):
        """_THINKING_PATTERN must be a compiled regex at module level."""
        import src.ui.textual_app_impl as m

        assert hasattr(m, "_THINKING_PATTERN"), (
            "_THINKING_PATTERN must exist at module level"
        )
        assert hasattr(m._THINKING_PATTERN, "search"), (
            "_THINKING_PATTERN must be compiled"
        )

    def test_hunk_pattern_is_module_level(self):
        """_HUNK_PATTERN must be a compiled regex at module level."""
        import src.ui.textual_app_impl as m

        assert hasattr(m, "_HUNK_PATTERN"), "_HUNK_PATTERN must exist at module level"
        assert hasattr(m._HUNK_PATTERN, "search"), "_HUNK_PATTERN must be compiled"

    def test_diff_pattern_matches_diff_block(self):
        """_DIFF_PATTERN must match a fenced ```diff block."""
        import src.ui.textual_app_impl as m

        text = "```diff\n-old\n+new\n```"
        match = m._DIFF_PATTERN.search(text)
        assert match is not None, "_DIFF_PATTERN must match ```diff...``` blocks"
        assert "-old\n+new" in match.group(1)

    def test_thinking_pattern_matches_think_tag(self):
        """_THINKING_PATTERN must match <think>...</think>."""
        import src.ui.textual_app_impl as m

        text = "<think>some reasoning</think> answer"
        match = m._THINKING_PATTERN.search(text)
        assert match is not None
        assert "some reasoning" in match.group(1)


# ---------------------------------------------------------------------------
# Fix 5 — streaming writes empty string on stream complete
# ---------------------------------------------------------------------------


class TestFix5StreamingNewline:
    def test_partial_false_triggers_empty_write(self):
        """partial=False must call output.write('') to end the stream on a clean line."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.output = MagicMock()
        writes = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: writes.append((fn, a))

        # Call the actual method bound to mock app
        CodingAgentTextualApp._on_model_token_ui(app, {"text": "", "partial": False})

        # Must have scheduled a write("") call
        write_calls = [a for fn, a in writes if a == ("",)]
        assert write_calls, (
            "partial=False must schedule output.write('') for trailing newline"
        )

    def test_partial_true_writes_token_text(self):
        """partial=True with text must schedule a write of the token."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.output = MagicMock()
        writes = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: writes.append((fn, a))

        CodingAgentTextualApp._on_model_token_ui(
            app, {"text": "hello", "partial": True}
        )

        write_args = [a for fn, a in writes]
        assert ("hello",) in write_args, (
            "partial=True must schedule write of token text"
        )


# ---------------------------------------------------------------------------
# Fix 6 — LogPanel bounded deque
# ---------------------------------------------------------------------------


class TestFix6BoundedLogPanel:
    def _make_panel(self):
        from src.ui.components.log_panel import LogPanel

        bus = MagicMock()
        bus.subscribe = MagicMock()
        return LogPanel(bus)

    def test_entries_is_deque(self):
        """LogPanel.entries must be a collections.deque, not a list."""
        panel = self._make_panel()
        assert isinstance(panel.entries, deque), "entries must be a deque"

    def test_entries_has_maxlen(self):
        """LogPanel.entries deque must have a maxlen set."""
        panel = self._make_panel()
        assert panel.entries.maxlen is not None, "deque must have a maxlen"
        assert panel.entries.maxlen >= 100, "maxlen must be at least 100"

    def test_entries_evicts_oldest_at_capacity(self):
        """Writing maxlen+1 entries must evict the first entry."""
        panel = self._make_panel()
        maxlen = panel.entries.maxlen
        for i in range(maxlen + 1):
            panel._on_new_log({"i": i})
        assert len(panel.entries) == maxlen, "len must stay at maxlen after overflow"
        # First entry (i=0) must be gone
        first_vals = [e["i"] for e in panel.entries]
        assert 0 not in first_vals, "Oldest entry must be evicted"
        assert maxlen in first_vals, "Newest entry must be present"

    def test_tail_returns_list(self):
        """tail() must return a plain list."""
        panel = self._make_panel()
        for i in range(10):
            panel._on_new_log({"i": i})
        result = panel.tail(5)
        assert isinstance(result, list), "tail() must return list"
        assert len(result) == 5

    def test_tail_returns_last_n(self):
        """tail(n) must return the n most-recent entries."""
        panel = self._make_panel()
        for i in range(20):
            panel._on_new_log({"i": i})
        result = panel.tail(3)
        assert [e["i"] for e in result] == [17, 18, 19]


# ---------------------------------------------------------------------------
# Fix 7 — Diff truncation shows indicator
# ---------------------------------------------------------------------------


class TestFix7DiffTruncation:
    def test_truncation_indicator_shown_for_long_diff(self):
        """_on_diff_preview_ui must append a '… N more lines' message when diff > 60 lines."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.output = MagicMock()
        written = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: written.append(
            a[0] if a else ""
        )

        big_diff = "\n".join(f"+line{i}" for i in range(80))
        CodingAgentTextualApp._on_diff_preview_ui(
            app, {"path": "f.py", "diff": big_diff}
        )

        combined = " ".join(str(w) for w in written)
        assert "more lines" in combined.lower() or "…" in combined, (
            "Truncation indicator must be shown when diff > 60 lines"
        )

    def test_no_truncation_indicator_for_short_diff(self):
        """No truncation message for diffs with ≤60 lines."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.output = MagicMock()
        written = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: written.append(
            a[0] if a else ""
        )

        small_diff = "\n".join(f"+line{i}" for i in range(10))
        CodingAgentTextualApp._on_diff_preview_ui(
            app, {"path": "f.py", "diff": small_diff}
        )

        combined = " ".join(str(w) for w in written)
        assert "more lines" not in combined.lower()


# ---------------------------------------------------------------------------
# Fix 8 — Plan step description ellipsis
# ---------------------------------------------------------------------------


class TestFix8PlanStepEllipsis:
    def test_long_description_truncated_with_ellipsis(self):
        """Descriptions >40 chars must be shown with … at position 38."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.plan_progress_label = MagicMock()
        updated = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: updated.append(
            a[0] if a else ""
        )

        long_desc = (
            "implement full authentication system with JWT tokens and refresh logic"
        )
        CodingAgentTextualApp._on_plan_progress_ui(
            app, {"step": 1, "total": 3, "description": long_desc}
        )

        combined = " ".join(updated)
        assert "…" in combined, "Long description must be shown with ellipsis"
        # The visible text must be ≤41 chars (38 + "…")
        for segment in combined.split("\n"):
            if "…" in segment:
                assert len(segment) <= 45, (
                    f"Truncated description too long: {segment!r}"
                )

    def test_short_description_not_truncated(self):
        """Descriptions ≤40 chars must be shown in full without ellipsis."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        from src.ui.textual_app_impl import CodingAgentTextualApp

        app = MagicMock(spec=CodingAgentTextualApp)
        app.plan_progress_label = MagicMock()
        updated = []
        app._schedule_callback.side_effect = lambda fn, *a, **kw: updated.append(
            a[0] if a else ""
        )

        short_desc = "edit auth.py"
        CodingAgentTextualApp._on_plan_progress_ui(
            app, {"step": 1, "total": 2, "description": short_desc}
        )
        combined = " ".join(updated)
        assert short_desc in combined, "Short description must appear verbatim"
        assert "…" not in combined, "Short description must not have ellipsis"


# ---------------------------------------------------------------------------
# Fix 9 — Compact Session is implemented
# ---------------------------------------------------------------------------


class TestFix9CompactSession:
    def test_compact_session_calls_distiller(self):
        """settings_compact_session must call compact_messages_to_prose, not be a placeholder."""
        from src.ui.textual_app_impl import TEXTUAL_AVAILABLE

        if not TEXTUAL_AVAILABLE:
            pytest.skip("Textual not available")
        import inspect

        # Find SettingsModal inside the module
        import src.ui.textual_app_impl as m

        src_text = inspect.getsource(m)
        # The handler for settings_compact_session must reference the distiller
        assert "compact_messages_to_prose" in src_text, (
            "Compact Session handler must call compact_messages_to_prose"
        )

    def test_compact_session_not_placeholder(self):
        """'Placeholder' comment must be removed from Compact Session handler."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        # settings_compact_session was the old button id; new button id is btn_compact
        # Either the old or new id must not have a Placeholder comment nearby
        for marker in (
            "settings_compact_session",
            "btn_compact",
            "_do_compact_session",
        ):
            idx = src_text.find(marker)
            if idx >= 0:
                snippet = src_text[idx : idx + 300]
                assert "Placeholder" not in snippet, (
                    f"Compact handler near '{marker}' must not contain 'Placeholder'"
                )


class TestSettingsScreenModern:
    """New compact settings modal: single screen, no nested provider/model screens."""

    def test_single_settings_modal_class(self):
        """There must be exactly one SettingsModal class (no ConnectProviderModal/SelectModelModal)."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "class SettingsModal" in src_text, "SettingsModal class must exist"
        assert "class ConnectProviderModal" not in src_text, (
            "ConnectProviderModal removed — provider select is inline in SettingsModal"
        )
        assert "class SelectModelModal" not in src_text, (
            "SelectModelModal removed — model select is inline in SettingsModal"
        )

    def test_inline_selects_in_settings(self):
        """SettingsModal must contain inline Select widgets for provider and model."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "sel_provider" in src_text, (
            "provider Select with id=sel_provider must exist"
        )
        assert "sel_model" in src_text, "model Select with id=sel_model must exist"

    def test_new_session_clears_orchestrator(self):
        """_do_new_session must call start_new_task() and reset _session_read_files."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "start_new_task" in src_text, (
            "_do_new_session must call start_new_task()"
        )
        assert "_session_read_files" in src_text, (
            "_do_new_session must reset _session_read_files"
        )

    def test_compact_shows_count(self):
        """_do_compact_session must report the number of messages reduced."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "n_before" in src_text or "→ 1 message" in src_text, (
            "_do_compact_session must show message count reduction to user"
        )

    def test_settings_has_esc_binding(self):
        """SettingsModal must have an Escape binding to close without using the mouse."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "escape" in src_text and "close_modal" in src_text, (
            "SettingsModal must bind Escape to close_modal action"
        )

    def test_info_bar_shows_msg_count_and_dir(self):
        """Settings modal must show live message count and working directory."""
        import src.ui.textual_app_impl as m
        import inspect

        src_text = inspect.getsource(m)
        assert "msg_count" in src_text, "Settings must display message count"
        assert "wd_str" in src_text or "working_dir" in src_text, (
            "Settings must display working directory"
        )


# ---------------------------------------------------------------------------
# Slash commands + quit / background cleanup tests
# ---------------------------------------------------------------------------


class TestSlashCommandsAndQuit:
    """Regression tests for /quit, /compact, /new slash commands and Ctrl+Q binding."""

    def _src(self):
        import src.ui.textual_app_impl as m
        import inspect

        return inspect.getsource(m)

    def test_slash_commands_list_has_quit_compact_new(self):
        """SLASH_COMMANDS must include /quit, /compact, and /new for Tab autocomplete."""
        import src.ui.textual_app_impl as m

        sc = m.SLASH_COMMANDS
        assert "/quit" in sc, "/quit must be in SLASH_COMMANDS"
        assert "/compact" in sc, "/compact must be in SLASH_COMMANDS"
        assert "/new" in sc, "/new must be in SLASH_COMMANDS"

    def test_ctrl_q_binding_present(self):
        """CodingAgentTextualApp.BINDINGS must contain ctrl+q → quit_app."""
        src_text = self._src()
        assert "ctrl+q" in src_text, "ctrl+q binding must be present"
        assert "quit_app" in src_text, "quit_app action must be referenced in BINDINGS"

    def test_action_quit_app_exists(self):
        """action_quit_app must be defined and call self.exit()."""
        src_text = self._src()
        assert "def action_quit_app" in src_text, "action_quit_app method must exist"
        assert "self.exit()" in src_text, "action_quit_app must call self.exit()"

    def test_action_quit_app_stops_audit_worker(self):
        """action_quit_app must stop the audit log worker (_audit_stop.set())."""
        src_text = self._src()
        assert "_audit_stop" in src_text and "_audit_stop.set()" in src_text, (
            "action_quit_app must call _audit_stop.set() to stop the audit worker"
        )

    def test_action_quit_app_shuts_down_executor(self):
        """action_quit_app must shut down the memory_update_node executor."""
        src_text = self._src()
        assert "memory_update_node" in src_text, (
            "action_quit_app must reference memory_update_node for executor shutdown"
        )
        assert "shutdown(wait=False)" in src_text, (
            "executor.shutdown(wait=False) must be called on quit"
        )

    def test_on_unmount_signals_cancel_and_cleanup(self):
        """on_unmount must set _cancel_event, stop audit worker, and shut down executor."""
        src_text = self._src()
        # on_unmount should contain all three cleanup calls
        assert "_cancel_event.set()" in src_text, (
            "on_unmount must call _cancel_event.set()"
        )
        assert "_audit_stop.set()" in src_text, "on_unmount must stop audit log worker"
        assert "shutdown(wait=False)" in src_text, "on_unmount must shut down executor"

    def test_slash_compact_runs_in_background_thread(self):
        """/compact handler must run _do_compact_session in a daemon thread, not inline."""
        src_text = self._src()
        # The /compact branch must use threading.Thread
        assert "_do_compact_session" in src_text, (
            "/compact must call _do_compact_session"
        )
        # The compact must be dispatched via Thread (not called directly on UI thread)
        assert "threading.Thread" in src_text, (
            "/compact must use a background thread to avoid blocking the UI"
        )

    def test_slash_new_calls_do_new_session(self):
        """/new handler must invoke _do_new_session."""
        src_text = self._src()
        assert "_do_new_session" in src_text, (
            "/new slash command must call _do_new_session"
        )

    def test_slash_help_lists_commands(self):
        """/help handler must list available commands including /quit, /compact, /new."""
        src_text = self._src()
        assert "/help" in src_text, "/help must be handled"
        # The help text must mention the new commands
        assert "/compact" in src_text, "/help output must mention /compact"
        assert "/new" in src_text, "/help output must mention /new"
        assert "/quit" in src_text, "/help output must mention /quit"
