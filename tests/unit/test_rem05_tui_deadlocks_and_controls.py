"""Regression tests for REM-05: Live TUI deadlocks, crashes, and broken controls."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App, ComposeResult

from tui.tui_src.ui._bridge_agent import TUI_ROLE_TO_PROMPT
from tui.tui_src.ui._bridge_context import BridgeContextMixin
from tui.tui_src.ui._bridge_provider_actions import update_role_model_impl
from tui.tui_src.ui._bridge_session import BridgeSessionMixin
from tui.tui_src.ui.components.bash_block import BashBlock
from tui.tui_src.ui.components.stream_view import StreamView
from tui.tui_src.ui.features.settings.screen import SettingsScreen, _safe_dom_id
from tui.tui_src.ui.screens.session_screen import SessionScreen


class TestUndoDeadlockAndSync:
    def test_undo_last_user_message_does_not_deadlock_and_syncs_msg_mgr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "tui.tui_src.ui._bridge_session.HISTORY_PATH",
            tmp_path / "history.json",
        )

        class _DummyBridge(BridgeSessionMixin):
            def __init__(self) -> None:
                self._history_lock = threading.Lock()
                self.history = [("user", "first"), ("assistant", "a1"), ("user", "second")]
                self._orchestrator = SimpleNamespace(
                    _msg_mgr_lock=threading.Lock(),
                    msg_mgr=SimpleNamespace(
                        messages=[
                            {"role": "user", "content": "first"},
                            {"role": "assistant", "content": "a1"},
                            {"role": "user", "content": "second"},
                            {"role": "assistant", "content": "a2"},
                        ]
                    ),
                )

        bridge = _DummyBridge()
        done = threading.Event()
        result: dict[str, bool] = {}

        def _worker() -> None:
            result["ok"] = bridge.undo_last_user_message()
            done.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        assert done.wait(timeout=2.0), "undo_last_user_message deadlocked on _history_lock"
        assert result["ok"] is True
        assert [m["content"] for m in bridge._orchestrator.msg_mgr.messages] == ["first", "a1"]


class TestStreamViewAndBashBlockLive:
    @pytest.mark.asyncio
    async def test_stream_view_stop_stream_releases_lock_before_await(self) -> None:
        class _Host(App):
            def compose(self) -> ComposeResult:
                yield StreamView(id="sv")

        app = _Host()
        async with app.run_test() as pilot:
            sv = app.query_one("#sv", StreamView)
            sv.append_chunk("partial output")
            await pilot.pause()

            # Replace with divergent final_content (previously held _flush_lock across await!)
            final = await asyncio.wait_for(
                sv.stop_stream(final_content="completely replaced final output"),
                timeout=2.0,
            )
            assert final == "completely replaced final output"
            assert sv._finalized is True
            # Lock must not remain held
            assert sv._flush_lock.acquire(blocking=False)
            sv._flush_lock.release()

    @pytest.mark.asyncio
    async def test_bash_block_escapes_rich_markup_in_title(self) -> None:
        class _Host(App):
            def compose(self) -> ComposeResult:
                yield BashBlock(
                    description="run [bold red]tests[/unclosed",
                    command="pytest tests/test_a.py[param-1]",
                )

        app = _Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            blk = app.query_one(BashBlock)
            assert "[bold red]" in blk._title_text()
            assert "[/unclosed" in blk._title_text()


class TestSessionScreenFocusAndSort:
    @pytest.mark.asyncio
    async def test_session_screen_sorts_by_timestamp_and_forks_while_search_focused(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "session_old.json").write_text(
            json.dumps({"task_name": "older", "timestamp": 1000, "message_count": 2}),
            encoding="utf-8",
        )
        (tmp_path / "session_fork_9999.json").write_text(
            json.dumps({"task_name": "newest fork", "timestamp": 9999, "message_count": 4}),
            encoding="utf-8",
        )

        class _Host(App):
            def _get_sessions_dir(self) -> Path:
                return tmp_path

        app = _Host()
        async with app.run_test() as pilot:
            scr = SessionScreen()
            app.push_screen(scr)
            await pilot.pause()
            # Newest timestamp must sort first regardless of filename
            assert scr._all_sessions[0][1]["task_name"] == "newest fork"
            # Move selection to first session (past 'Start new session') while search Input has focus
            await pilot.press("down")
            await pilot.press("ctrl+f")
            await pilot.pause()
            fork_files = list(tmp_path.glob("session_fork_*.json"))
            assert len(fork_files) >= 2


class TestSettingsAndProviderSwitching:
    def test_safe_dom_id_sanitizes_invalid_chars(self) -> None:
        assert _safe_dom_id("openai.compat/v1") == "openai_compat_v1"

    def test_update_role_model_ignores_none_sentinel(self) -> None:
        adapter = SimpleNamespace(default_model="gpt-4o", models=["gpt-4o"])
        orch = SimpleNamespace(_adapter=adapter, msg_mgr=SimpleNamespace(messages=[]))
        bridge = SimpleNamespace(_orchestrator=orch, activate_provider=MagicMock())
        update_role_model_impl(bridge, "none", provider_id="none")
        assert adapter.default_model == "gpt-4o"
        bridge.activate_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_screen_do_save_skips_none_and_passes_provider_id(self) -> None:
        store = SimpleNamespace(
            available_providers=[
                {"name": "OpenAI.Compat", "type": "openai_compat", "models": ["gpt-4o"], "base_url": "http://localhost:8000/v1"}
            ],
            get=lambda k, d=None: d,
            get_api_key=lambda pid: "",
            update=MagicMock(),
            save=MagicMock(),
        )
        posted: list[object] = []

        class _Host(App):
            def on_update_role_model(self, event) -> None:
                posted.append(event)

        app = _Host()
        async with app.run_test() as pilot:
            scr = SettingsScreen(store)
            app.push_screen(scr)
            await pilot.pause()
            # Default selections are "none" — saving must NOT post UpdateRoleModel("none")
            scr._do_save()
            await pilot.pause()
            assert posted == []
            saved_updates = store.update.call_args[0][0]
            assert all(v != "none" for v in saved_updates.values())


class TestSlashCommandsBridgeAndAppControls:
    @pytest.mark.asyncio
    async def test_slash_queue_preserves_deque_and_theme_saves(self) -> None:
        from tui.tui_src.ui._slash_model_commands_mixin import _SlashModelCommandsMixin

        class _FakeApp(_SlashModelCommandsMixin):
            def __init__(self) -> None:
                self._queued_messages: deque[str] = deque(["first"])
                self._settings = SimpleNamespace(
                    update=MagicMock(),
                    save=MagicMock(),
                )
                self.available_themes = {"nord": object(), "textual-dark": object()}
                self.theme = "textual-dark"
                self.mounted: list[object] = []

            async def _mount_chat_widget(self, w) -> None:
                self.mounted.append(w)

            def _update_queue_indicator(self) -> None:
                pass

        fake = _FakeApp()
        await fake._slash_queue("second")
        assert isinstance(fake._queued_messages, deque)
        assert list(fake._queued_messages) == ["first", "second"]
        assert fake._queued_messages.popleft() == "first"

        await fake._slash_theme("nord")
        fake._settings.update.assert_called_once_with({"theme": "nord"})
        fake._settings.save.assert_called_once()

    def test_strategic_role_maps_to_strategic_prompt(self) -> None:
        assert TUI_ROLE_TO_PROMPT["strategic"] == "strategic"
        assert TUI_ROLE_TO_PROMPT["operational"] == "operational"
        assert TUI_ROLE_TO_PROMPT["reviewer"] == "reviewer"

    def test_compact_context_respects_bool_and_ignores_flush_execution_trace(self) -> None:
        class _Bridge(BridgeContextMixin):
            def __init__(self, orch) -> None:
                self._orchestrator = orch

        only_flush = _Bridge(SimpleNamespace(flush_execution_trace=MagicMock()))
        assert only_flush.compact_context() is False
        only_flush._orchestrator.flush_execution_trace.assert_not_called()

        returns_false = _Bridge(SimpleNamespace(compact_context=MagicMock(return_value=False)))
        assert returns_false.compact_context() is False

        returns_true = _Bridge(SimpleNamespace(compact_context=MagicMock(return_value=True)))
        assert returns_true.compact_context() is True

    @pytest.mark.asyncio
    async def test_role_transition_handler_imports_status_bar_cleanly(self) -> None:
        from tui.tui_src.ui._app_status_handlers_mixin import AppStatusHandlersMixin
        from tui.tui_src.ui.bus import RoleTransitionEvent

        class _FakeApp(AppStatusHandlersMixin):
            def __init__(self) -> None:
                self.active_role = "operational"
                self._bridge = SimpleNamespace(set_active_role=MagicMock())
                self.mounted: list[object] = []

            def _finalize_stream(self, *, detach: bool = False) -> None:
                pass

            def _update_role_display(self, role: str) -> None:
                pass

            def _update_status_bar(self) -> None:
                pass

            async def _mount_chat_widget(self, w) -> None:
                self.mounted.append(w)

        fake = _FakeApp()
        await fake.handle_role_transition(
            RoleTransitionEvent(from_role="operational", to_role="strategic")
        )
        assert fake.active_role == "strategic"
        assert len(fake.mounted) == 1
        fake._bridge.set_active_role.assert_called_once_with("strategic")
