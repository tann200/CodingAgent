"""Tests for CP-15 — send_user_message tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from src.tools.interaction_tools import send_user_message

# The event bus is imported lazily inside send_user_message, so we patch
# the import target in the event_bus module itself.
_BUS_PATCH = "src.core.orchestration.event_bus.get_event_bus"


class TestSendUserMessage:
    def test_empty_message_returns_error(self) -> None:
        result = send_user_message("")
        assert result["status"] == "error"
        assert "empty" in result["error"]

    def test_whitespace_only_message_returns_error(self) -> None:
        result = send_user_message("   ")
        assert result["status"] == "error"

    def test_success_returns_ok(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            result = send_user_message("Hello from agent")
        assert result["status"] == "ok"
        assert result["ok"] is True
        assert result["delivered"] is True

    def test_message_stripped_in_result(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            result = send_user_message("  Trimmed  ")
        assert result["message"] == "Trimmed"

    def test_publishes_agent_message_event(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            send_user_message("Progress update", status="proactive")
        mock_bus.publish.assert_called_once()
        event_name, payload = mock_bus.publish.call_args[0]
        assert event_name == "agent.message"
        assert payload["message"] == "Progress update"
        assert payload["status"] == "proactive"

    def test_default_status_is_normal(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            send_user_message("Hi")
        _, payload = mock_bus.publish.call_args[0]
        assert payload["status"] == "normal"

    def test_attachments_forwarded(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            send_user_message("msg", attachments=["file.py", "README.md"])
        _, payload = mock_bus.publish.call_args[0]
        assert payload["attachments"] == ["file.py", "README.md"]

    def test_attachments_default_empty_list(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            send_user_message("msg")
        _, payload = mock_bus.publish.call_args[0]
        assert payload["attachments"] == []

    def test_event_bus_unavailable_degrades_gracefully(self) -> None:
        with patch(_BUS_PATCH, side_effect=RuntimeError("no bus")):
            result = send_user_message("Still works")
        # Should still return ok even without event bus
        assert result["status"] == "ok"
        assert result["delivered"] is True

    def test_proactive_status_payload(self) -> None:
        with patch(_BUS_PATCH) as mock_get:
            mock_bus = MagicMock()
            mock_get.return_value = mock_bus
            result = send_user_message("Proactive note", status="proactive")
        assert result["status"] == "ok"
        _, payload = mock_bus.publish.call_args[0]
        assert payload["status"] == "proactive"

    def test_registered_in_tool_permissions(self) -> None:
        from src.tools.tools_config import TOOL_PERMISSIONS, PermissionLevel

        assert "send_user_message" in TOOL_PERMISSIONS
        assert TOOL_PERMISSIONS["send_user_message"] == PermissionLevel.READ_ONLY

    def test_has_tool_meta(self) -> None:
        from src.tools._tool import TOOL_ATTR

        assert hasattr(send_user_message, TOOL_ATTR)
        meta = getattr(send_user_message, TOOL_ATTR)
        assert meta.name == "send_user_message"
