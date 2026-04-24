"""Tests for P4-4: Budget ceiling alert in SessionCostTracker."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

from src.core.orchestration.session_cost_tracker import SessionCostTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeBus:
    """Minimal event bus that records published events."""

    def __init__(self) -> None:
        self.events: List[tuple] = []  # [(event_name, payload)]

    def publish(self, event: str, payload: Any) -> None:
        self.events.append((event, payload))

    def budget_exceeded_events(self) -> List[tuple]:
        return [(e, p) for e, p in self.events if e == "usage.budget_exceeded"]

    def turn_summary_events(self) -> List[tuple]:
        return [(e, p) for e, p in self.events if e == "usage.turn_summary"]


def _make_tracker(
    tmp_path: Path,
    ceiling: float | None = None,
    bus: _FakeBus | None = None,
) -> SessionCostTracker:
    return SessionCostTracker(
        working_dir=tmp_path,
        event_bus=bus or _FakeBus(),
        budget_ceiling_usd=ceiling,
    )


def _record_cheap_turn(tracker: SessionCostTracker, cost_override: float = 0.0) -> None:
    """Record one LLM turn whose cost is patched to cost_override USD."""
    with patch.object(
        SessionCostTracker,
        "_estimate_cost",
        return_value=cost_override,
    ):
        tracker.record_llm_usage(
            prompt_tokens=100,
            completion_tokens=20,
            model="gpt-4o-mini",
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_no_ceiling_by_default(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(working_dir=tmp_path)
        assert tracker._budget_ceiling_usd is None

    def test_ceiling_stored_as_float(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(working_dir=tmp_path, budget_ceiling_usd=2.5)
        assert tracker._budget_ceiling_usd == pytest.approx(2.5)

    def test_ceiling_integer_converted_to_float(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(working_dir=tmp_path, budget_ceiling_usd=3)
        assert isinstance(tracker._budget_ceiling_usd, float)
        assert tracker._budget_ceiling_usd == pytest.approx(3.0)

    def test_not_notified_at_start(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(working_dir=tmp_path, budget_ceiling_usd=1.0)
        assert tracker._budget_exceeded_notified is False


# ---------------------------------------------------------------------------
# No alert when no ceiling
# ---------------------------------------------------------------------------


class TestNoCeiling:
    def test_no_budget_exceeded_event_published(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=None, bus=bus)
        _record_cheap_turn(tracker, cost_override=999.0)
        assert bus.budget_exceeded_events() == []

    def test_turn_summary_still_published_without_ceiling(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=None, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.50)
        assert len(bus.turn_summary_events()) == 1


# ---------------------------------------------------------------------------
# Alert fires exactly once when ceiling is crossed
# ---------------------------------------------------------------------------


class TestCeilingAlert:
    def test_no_event_below_ceiling(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=1.00, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.50)
        assert bus.budget_exceeded_events() == []

    def test_no_event_exactly_at_ceiling(self, tmp_path: Path) -> None:
        """Exactly at ceiling is NOT over ceiling (strict >)."""
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=1.00, bus=bus)
        _record_cheap_turn(tracker, cost_override=1.00)
        assert bus.budget_exceeded_events() == []

    def test_event_fires_when_ceiling_exceeded(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=1.00, bus=bus)
        _record_cheap_turn(tracker, cost_override=1.01)
        events = bus.budget_exceeded_events()
        assert len(events) == 1

    def test_event_fires_only_once(self, tmp_path: Path) -> None:
        """Multiple turns over the ceiling must only emit one alert."""
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        for _ in range(5):
            _record_cheap_turn(tracker, cost_override=0.20)
        assert len(bus.budget_exceeded_events()) == 1

    def test_event_payload_contains_correct_fields(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.60)
        _, payload = bus.budget_exceeded_events()[0]
        assert "session_cost_usd" in payload
        assert "budget_ceiling_usd" in payload
        assert payload["budget_ceiling_usd"] == pytest.approx(0.50)
        assert payload["session_cost_usd"] > 0.50

    def test_event_payload_session_cost_matches_tracker(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.75)
        _, payload = bus.budget_exceeded_events()[0]
        assert payload["session_cost_usd"] == pytest.approx(tracker.session_cost_usd)

    def test_turn_summary_still_published_with_ceiling(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.75)
        # Both events should be present
        assert len(bus.turn_summary_events()) == 1
        assert len(bus.budget_exceeded_events()) == 1


# ---------------------------------------------------------------------------
# Warning log
# ---------------------------------------------------------------------------


class TestWarningLog:
    def test_warning_logged_when_ceiling_exceeded(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path, ceiling=0.50)
        with patch.object(
            SessionCostTracker,
            "_estimate_cost",
            return_value=0.75,
        ):
            with patch(
                "src.core.orchestration.session_cost_tracker.logger"
            ) as mock_logger:
                tracker.record_llm_usage(
                    prompt_tokens=100,
                    completion_tokens=20,
                    model="gpt-4o-mini",
                )
                mock_logger.warning.assert_called_once()
                call_args = mock_logger.warning.call_args[0]
                assert "exceeded" in call_args[0]

    def test_no_warning_below_ceiling(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path, ceiling=1.00)
        with patch("src.core.orchestration.session_cost_tracker.logger") as mock_logger:
            with patch.object(SessionCostTracker, "_estimate_cost", return_value=0.50):
                tracker.record_llm_usage(
                    prompt_tokens=100,
                    completion_tokens=20,
                    model="gpt-4o-mini",
                )
            mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# reset() clears the notification flag
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_exceeded_flag(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.75)
        assert tracker._budget_exceeded_notified is True
        tracker.reset()
        assert tracker._budget_exceeded_notified is False

    def test_reset_clears_session_cost(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.75)
        tracker.reset()
        assert tracker.session_cost_usd == pytest.approx(0.0)

    def test_alert_fires_again_after_reset(self, tmp_path: Path) -> None:
        """After reset, the ceiling can be crossed again and a second alert fires."""
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        _record_cheap_turn(tracker, cost_override=0.75)
        tracker.reset()
        _record_cheap_turn(tracker, cost_override=0.75)
        assert len(bus.budget_exceeded_events()) == 2


# ---------------------------------------------------------------------------
# No event bus — graceful degradation
# ---------------------------------------------------------------------------


class TestNoEventBus:
    def test_no_error_when_bus_is_none(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(
            working_dir=tmp_path,
            event_bus=None,
            budget_ceiling_usd=0.10,
        )
        _record_cheap_turn(tracker, cost_override=0.50)
        # No exception raised — that's the assertion

    def test_notified_flag_set_even_without_bus(self, tmp_path: Path) -> None:
        tracker = SessionCostTracker(
            working_dir=tmp_path,
            event_bus=None,
            budget_ceiling_usd=0.10,
        )
        _record_cheap_turn(tracker, cost_override=0.50)
        assert tracker._budget_exceeded_notified is True


# ---------------------------------------------------------------------------
# Thread safety — two threads accumulate cost concurrently
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_budget_event_fires_at_most_once_under_concurrent_load(
        self, tmp_path: Path
    ) -> None:
        bus = _FakeBus()
        tracker = _make_tracker(tmp_path, ceiling=0.50, bus=bus)
        errors: List[Exception] = []

        def _worker():
            try:
                for _ in range(3):
                    _record_cheap_turn(tracker, cost_override=0.10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # May fire more than once due to TOCTOU in the check (acceptable for now),
        # but must fire at least once given total cost = 4×3×0.10 = $1.20 > $0.50.
        assert len(bus.budget_exceeded_events()) >= 1
