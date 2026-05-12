"""Tests for EventBus stderr fallback subscriber."""

from src.core.orchestration.event_bus import EventBus, subscribe_stderr_fallback


def test_safety_event_printed_to_stderr(capsys):
    eb = EventBus()
    subscribe_stderr_fallback(eb)
    eb.publish("system.warning", {"message": "sandbox degraded"})
    captured = capsys.readouterr()
    assert "sandbox degraded" in captured.err


def test_doom_loop_event_printed_to_stderr(capsys):
    eb = EventBus()
    subscribe_stderr_fallback(eb)
    eb.publish("tool.doom_loop_detected", {"message": "doom loop!"})
    captured = capsys.readouterr()
    assert "doom loop!" in captured.err


def test_non_safety_event_not_printed_to_stderr(capsys):
    eb = EventBus()
    subscribe_stderr_fallback(eb)
    eb.publish("some.other.event", {"message": "normal event"})
    captured = capsys.readouterr()
    assert "normal event" not in captured.err
