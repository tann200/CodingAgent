"""Helpers for WebSocket control-message parsing and acknowledgements."""

from __future__ import annotations

import json
from typing import Any, Optional


def parse_websocket_control_message(text: str) -> Optional[dict[str, Any]]:
    """Parse a WebSocket control message, returning a dict or None."""
    try:
        msg = json.loads(text)
    except Exception:
        return None
    return msg if isinstance(msg, dict) else None


def build_control_subscribed_payload(event_name: str) -> tuple[str, dict[str, Any]]:
    return "_control", {"type": "subscribed", "event": event_name}


def build_control_error_payload(event_name: str) -> tuple[str, dict[str, Any]]:
    return "_control", {"type": "error", "event": event_name}


def build_control_unsubscribed_payload(
    event_name: str, *, was_subscribed: bool = True
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"type": "unsubscribed", "event": event_name}
    if not was_subscribed:
        payload["was_subscribed"] = False
    return "_control", payload


def build_control_subscriptions_payload(events: list[str]) -> tuple[str, dict[str, Any]]:
    return "_control", {"type": "subscriptions", "events": events}


def build_control_pong_payload() -> tuple[str, dict[str, Any]]:
    return "_control", {"type": "pong"}
