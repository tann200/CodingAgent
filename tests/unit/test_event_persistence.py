"""tests/unit/test_event_persistence.py — TASK-REL-3

Tests for src.core.orchestration.event_persistence — on-disk ACK pattern.
Covers: atomic persist, idempotent ACK, crash-recovery, stale pruning,
corrupt-file discard, PersistentEventBus.initialize() replay count.
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import src.core.orchestration.event_persistence as ep
from src.core.orchestration.event_persistence import (
    PersistentEventBus,
    ack_event,
    persist_event,
    recover_pending,
    set_pending_dir,
    PERSISTENT_EVENT_TYPES,
    DEFAULT_MAX_AGE_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_pending_dir(tmp_path: Path):
    """Each test gets its own pending directory."""
    pending = tmp_path / "pending"
    set_pending_dir(pending)
    yield pending
    # Cleanup: reset to None so subsequent tests see a fresh directory
    ep._pending_dir_override = None


# ---------------------------------------------------------------------------
# persist_event
# ---------------------------------------------------------------------------


def test_persist_event_creates_file(isolated_pending_dir: Path) -> None:
    path = persist_event("agent.response", {"correlation_id": "abc123", "text": "hi"})
    assert path.exists()
    assert path.suffix == ".json"


def test_persist_event_content_is_valid_json(isolated_pending_dir: Path) -> None:
    payload = {"correlation_id": "test-id", "value": 42}
    path = persist_event("agent.response", payload)
    data = json.loads(path.read_text())
    assert data["event_name"] == "agent.response"
    assert data["payload"]["correlation_id"] == "test-id"
    assert data["payload"]["value"] == 42
    assert "timestamp" in data


def test_persist_event_strips_underscore_keys(isolated_pending_dir: Path) -> None:
    payload = {
        "correlation_id": "x",
        "_recovered": True,
        "_internal": "do-not-store",
        "keep": "this",
    }
    path = persist_event("tool.result", payload)
    data = json.loads(path.read_text())
    stored = data["payload"]
    assert "_recovered" not in stored
    assert "_internal" not in stored
    assert stored["keep"] == "this"


def test_persist_event_creates_pending_dir_if_missing(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    set_pending_dir(deep)
    path = persist_event("agent.response", {"correlation_id": "id1"})
    assert path.exists()
    assert deep.is_dir()


def test_persist_event_filename_sanitized(isolated_pending_dir: Path) -> None:
    # Correlation IDs with special chars must produce safe filenames
    path = persist_event("agent.response", {"correlation_id": "abc/def:ghi"})
    assert "/" not in path.name
    assert ":" not in path.name


def test_persist_event_uses_correlation_id_in_name(isolated_pending_dir: Path) -> None:
    path = persist_event("agent.response", {"correlation_id": "myCorrelation"})
    assert "myCorrelation" in path.name


def test_persist_event_fallback_name_when_no_correlation(isolated_pending_dir: Path) -> None:
    path = persist_event("agent.response", {"data": "no corr id here"})
    assert "evt" in path.name


# ---------------------------------------------------------------------------
# ack_event
# ---------------------------------------------------------------------------


def test_ack_event_deletes_file(isolated_pending_dir: Path) -> None:
    path = persist_event("agent.response", {"correlation_id": "del-me"})
    assert path.exists()
    ack_event(path)
    assert not path.exists()


def test_ack_event_is_idempotent(isolated_pending_dir: Path) -> None:
    path = isolated_pending_dir / "phantom.json"
    # File does not exist — should not raise
    ack_event(path)  # no error
    ack_event(path)  # second call also safe


# ---------------------------------------------------------------------------
# recover_pending — happy path
# ---------------------------------------------------------------------------


def test_recover_pending_empty_dir(isolated_pending_dir: Path) -> None:
    result = recover_pending()
    assert result == []


def test_recover_pending_returns_unacked_events(isolated_pending_dir: Path) -> None:
    persist_event("agent.response", {"correlation_id": "r1", "text": "hello"})
    persist_event("agent.error", {"correlation_id": "r2", "error": "boom"})

    recovered = recover_pending()
    assert len(recovered) == 2
    for ev in recovered:
        assert ev["_recovered"] is True
        assert "_recovery_path" in ev
        assert Path(ev["_recovery_path"]).exists()


def test_recover_pending_tags_event_name(isolated_pending_dir: Path) -> None:
    persist_event("tool.result", {"correlation_id": "tag-me"})
    recovered = recover_pending()
    assert recovered[0].get("event") == "tool.result"


def test_recover_pending_empty_after_all_acked(isolated_pending_dir: Path) -> None:
    path = persist_event("agent.response", {"correlation_id": "ack-me"})
    ack_event(path)
    assert recover_pending() == []


def test_recover_pending_partial_ack(isolated_pending_dir: Path) -> None:
    p1 = persist_event("agent.response", {"correlation_id": "keep"})
    p2 = persist_event("agent.response", {"correlation_id": "remove"})
    ack_event(p2)

    recovered = recover_pending()
    assert len(recovered) == 1
    assert recovered[0]["correlation_id"] == "keep"


def test_recover_pending_recovery_path_enables_deferred_ack(isolated_pending_dir: Path) -> None:
    persist_event("agent.response", {"correlation_id": "deferred"})
    recovered = recover_pending()
    assert len(recovered) == 1

    # Simulate: client processes event, then ACKs via the stored path
    ack_event(Path(recovered[0]["_recovery_path"]))

    assert recover_pending() == []


# ---------------------------------------------------------------------------
# recover_pending — stale / corrupt files
# ---------------------------------------------------------------------------


def test_recover_pending_prunes_stale_files(isolated_pending_dir: Path) -> None:
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    stale = isolated_pending_dir / "0000000001_old.json"
    stale.write_text(json.dumps({
        "event_name": "agent.response",
        "payload": {"correlation_id": "old"},
        "timestamp": time.time() - 90_000,  # 25 h ago
    }))
    # Set mtime far in the past
    old_ts = time.time() - 90_000
    import os; os.utime(stale, (old_ts, old_ts))

    result = recover_pending(max_age_seconds=86_400)
    assert result == []
    assert not stale.exists()


def test_recover_pending_discards_corrupt_files(isolated_pending_dir: Path) -> None:
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    bad = isolated_pending_dir / "9999999999_corrupt.json"
    bad.write_text("{ this is not valid json !!!")

    result = recover_pending()
    assert result == []
    assert not bad.exists()


def test_recover_pending_skips_tmp_files(isolated_pending_dir: Path) -> None:
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    tmp = isolated_pending_dir / "partial.tmp"
    tmp.write_text("{}")
    # .tmp files must NOT appear in recovery (glob only picks *.json)
    result = recover_pending()
    assert result == []


# ---------------------------------------------------------------------------
# recover_pending — nonexistent directory
# ---------------------------------------------------------------------------


def test_recover_pending_missing_dir_returns_empty(tmp_path: Path) -> None:
    set_pending_dir(tmp_path / "does_not_exist")
    assert recover_pending() == []


# ---------------------------------------------------------------------------
# PERSISTENT_EVENT_TYPES filter
# ---------------------------------------------------------------------------


def test_persistent_event_types_contains_critical() -> None:
    assert "agent.response" in PERSISTENT_EVENT_TYPES
    assert "agent.error" in PERSISTENT_EVENT_TYPES
    assert "tool.result" in PERSISTENT_EVENT_TYPES
    assert "agent.final_response" in PERSISTENT_EVENT_TYPES
    assert "agent.stream_chunk" in PERSISTENT_EVENT_TYPES


def test_persistent_event_types_excludes_noise() -> None:
    # High-frequency / non-critical events must NOT be persisted
    assert "heartbeat" not in PERSISTENT_EVENT_TYPES
    assert "log.info" not in PERSISTENT_EVENT_TYPES


# ---------------------------------------------------------------------------
# PersistentEventBus
# ---------------------------------------------------------------------------


def test_persistent_event_bus_initialize_returns_count(isolated_pending_dir: Path) -> None:
    persist_event("agent.response", {"correlation_id": "b1"})
    persist_event("agent.error", {"correlation_id": "b2"})

    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    count = bus.initialize()
    assert count == 2


def test_persistent_event_bus_initialize_idempotent(isolated_pending_dir: Path) -> None:
    persist_event("agent.response", {"correlation_id": "idem"})
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    c1 = bus.initialize()
    c2 = bus.initialize()  # second call should return 0 (already initialized)
    assert c1 == 1
    assert c2 == 0


def test_persistent_event_bus_initialize_zero_when_no_pending(isolated_pending_dir: Path) -> None:
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    assert bus.initialize() == 0


def test_persistent_event_bus_persist_filter(isolated_pending_dir: Path) -> None:
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    # Non-persistent event type: no file should be written
    path = bus._persist_event("heartbeat", {"correlation_id": "hb"})
    assert path is None
    assert list(isolated_pending_dir.glob("*.json")) == []


def test_persistent_event_bus_persist_creates_file(isolated_pending_dir: Path) -> None:
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    path = bus._persist_event("agent.response", {"correlation_id": "ok"})
    assert path is not None
    assert path.exists()


def test_persistent_event_bus_ack_via_persistence_path(isolated_pending_dir: Path) -> None:
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    path = bus._persist_event("agent.response", {"correlation_id": "ack"})
    assert path is not None

    payload = {"_persistence_path": str(path)}
    acked = bus._ack_event(payload)
    assert acked is True
    assert not path.exists()


def test_persistent_event_bus_ack_returns_false_without_path(isolated_pending_dir: Path) -> None:
    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    assert bus._ack_event({}) is False
    assert bus._ack_event("not a dict") is False  # type: ignore[arg-type]


def test_persistent_event_bus_recover_publishes_events(isolated_pending_dir: Path) -> None:
    persist_event("agent.response", {"correlation_id": "pub1", "text": "recovered"})

    bus = PersistentEventBus(pending_dir=isolated_pending_dir)
    received: list[dict] = []
    bus.subscribe("agent.response", lambda payload: received.append(payload))

    count = bus.recover_pending()
    assert count == 1
    assert len(received) == 1
    assert received[0]["_recovered"] is True
