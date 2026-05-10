from src.server.event_subscriptions import (
    DEFAULT_SERVER_EVENT_TYPES,
    resolve_initial_websocket_events,
)


def test_default_server_event_types_contains_expected_core_events():
    assert "agent.start" in DEFAULT_SERVER_EVENT_TYPES
    assert "session.created" in DEFAULT_SERVER_EVENT_TYPES
    assert "perception.corrective_prompt" in DEFAULT_SERVER_EVENT_TYPES


def test_resolve_initial_websocket_events_defaults_and_none():
    assert resolve_initial_websocket_events(None) == list(DEFAULT_SERVER_EVENT_TYPES)
    assert resolve_initial_websocket_events("none") == []
    assert resolve_initial_websocket_events("   ") == []


def test_resolve_initial_websocket_events_supports_csv_and_all():
    assert resolve_initial_websocket_events("session.created, log") == [
        "session.created",
        "log",
    ]
    assert resolve_initial_websocket_events("session.created, all") == list(
        DEFAULT_SERVER_EVENT_TYPES
    )
