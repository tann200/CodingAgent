from __future__ import annotations

from unittest.mock import MagicMock


def _make_app():
    from tui.src.ui.app import AgentApp

    app = AgentApp.__new__(AgentApp)
    app._subagent_widgets = {}
    app._sched_chat_widget = MagicMock()
    app._update_subagent_footer = MagicMock()
    app.call_later = lambda fn: fn()
    status = MagicMock()
    app.query_one = MagicMock(return_value=status)
    return app, status


def test_subagent_start_registers_widget_and_updates_status() -> None:
    from tui.src.ui.app import AgentApp
    from tui.src.ui.bus import SubagentStartEvent

    app, status = _make_app()
    event = SubagentStartEvent("child-1", "analyst", "inspect auth flow")

    AgentApp.handle_subagent_start(app, event)

    assert app._subagent_widgets["child-1"]._child_session_id == "child-1"
    app._sched_chat_widget.assert_called_once()
    status.update.assert_called_once_with("1 running")
    app._update_subagent_footer.assert_called_once_with()


def test_subagent_start_reuses_existing_widget_for_same_child() -> None:
    from tui.src.ui.app import AgentApp
    from tui.src.ui.bus import SubagentStartEvent
    from tui.src.ui.components.subagent_progress import SubagentProgress

    app, status = _make_app()
    existing = SubagentProgress("analyst", "inspect auth flow", "child-1")
    app._subagent_widgets["child-1"] = existing

    AgentApp.handle_subagent_start(
        app, SubagentStartEvent("child-1", "analyst", "inspect auth flow")
    )

    assert app._subagent_widgets["child-1"] is existing
    app._sched_chat_widget.assert_not_called()
    status.update.assert_called_once_with("1 running")
    app._update_subagent_footer.assert_called_once_with()


def test_subagent_finish_removes_widget_and_marks_complete() -> None:
    from tui.src.ui.app import AgentApp
    from tui.src.ui.bus import SubagentFinishEvent

    app, status = _make_app()
    widget = MagicMock()
    app._subagent_widgets["child-1"] = widget

    AgentApp.handle_subagent_finish(
        app, SubagentFinishEvent("child-1", "analyst", True)
    )

    assert "child-1" not in app._subagent_widgets
    widget.finish.assert_called_once_with(True)
    status.update.assert_called_once_with("none")
    app._update_subagent_footer.assert_called_once_with()
