from src.server.websocket_control import (
    build_control_error_payload,
    build_control_pong_payload,
    build_control_subscribed_payload,
    build_control_subscriptions_payload,
    build_control_unsubscribed_payload,
    parse_websocket_control_message,
)


def test_parse_websocket_control_message_returns_dict_or_none():
    assert parse_websocket_control_message('{"type":"ping"}') == {"type": "ping"}
    assert parse_websocket_control_message("not-json") is None
    assert parse_websocket_control_message("[1,2,3]") is None


def test_build_control_payload_helpers_return_expected_envelopes():
    assert build_control_subscribed_payload("session.created") == (
        "_control",
        {"type": "subscribed", "event": "session.created"},
    )
    assert build_control_error_payload("session.created") == (
        "_control",
        {"type": "error", "event": "session.created"},
    )
    assert build_control_unsubscribed_payload("session.created") == (
        "_control",
        {"type": "unsubscribed", "event": "session.created"},
    )
    assert build_control_unsubscribed_payload(
        "session.created", was_subscribed=False
    ) == (
        "_control",
        {
            "type": "unsubscribed",
            "event": "session.created",
            "was_subscribed": False,
        },
    )
    assert build_control_subscriptions_payload(["session.created"]) == (
        "_control",
        {"type": "subscriptions", "events": ["session.created"]},
    )
    assert build_control_pong_payload() == ("_control", {"type": "pong"})
