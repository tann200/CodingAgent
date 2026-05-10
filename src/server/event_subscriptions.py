"""Helpers for default server event subscriptions and query parsing."""

from __future__ import annotations


DEFAULT_SERVER_EVENT_TYPES = [
    "agent.start",
    "agent.end",
    "tool.start",
    "tool.end",
    "mcp.server.status",
    "workflow.step",
    "llm.response",
    "llm.token",
    "session.created",
    "session.updated",
    "perception.corrective_prompt",
    "error",
    "log",
]


def resolve_initial_websocket_events(events_param: str | None) -> list[str]:
    """Resolve initial event subscriptions from the `events` query parameter."""
    if events_param is None:
        return list(DEFAULT_SERVER_EVENT_TYPES)

    if events_param.strip().lower() in ("", "none"):
        return []

    parsed = [event.strip() for event in events_param.split(",") if event.strip()]
    if "all" in [event.lower() for event in parsed]:
        return list(DEFAULT_SERVER_EVENT_TYPES)
    return parsed
