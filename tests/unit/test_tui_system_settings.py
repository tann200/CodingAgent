from __future__ import annotations

from unittest.mock import MagicMock


def test_request_system_settings_delegates_to_bridge() -> None:
    from tui.src.ui.app import AgentApp

    app = AgentApp.__new__(AgentApp)
    app._bridge = MagicMock()

    AgentApp.handle_request_system_settings(app, None)

    app._bridge._publish_system_settings.assert_called_once_with()
