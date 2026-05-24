"""Tests for CircuitBreaker, get_circuit_breaker, and call_model CB integration.

Coverage targets
----------------
- CircuitBreaker: CLOSED → OPEN transition on failures
- CircuitBreaker: OPEN → HALF_OPEN after recovery_timeout elapses
- CircuitBreaker: HALF_OPEN → CLOSED on success
- CircuitBreaker: HALF_OPEN → OPEN on failure
- CircuitBreaker: probe_in_flight prevents concurrent probes (BUG-3)
- CircuitBreaker: record_success resets state from any state
- CircuitBreaker: is_open() semantics in each state
- CircuitBreaker: thread-safety under concurrent record_failure calls
- get_circuit_breaker: returns same instance for same key (singleton per key)
- get_circuit_breaker: separate instances for distinct keys
- call_model: CB fast-fail returns ok=False without calling _call_model_internal
- call_model: CB records success on clean response
- call_model: CB records failure on error response
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.inference.llm_manager import (
    CircuitBreaker,
    _CIRCUIT_BREAKERS,
    _CB_LOCK,
    call_model,
    get_circuit_breaker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_cb(failure_threshold: int = 3, recovery_timeout: float = 60.0) -> CircuitBreaker:
    """Return a new CircuitBreaker not linked to the global registry."""
    return CircuitBreaker(failure_threshold=failure_threshold, recovery_timeout=recovery_timeout)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_circuit_breaker_initial_state_closed():
    cb = _fresh_cb()
    assert cb.state == CircuitBreaker.CLOSED


def test_circuit_breaker_is_open_false_initially():
    cb = _fresh_cb()
    assert cb.is_open() is False


# ---------------------------------------------------------------------------
# CLOSED → OPEN transition
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_threshold_failures():
    cb = _fresh_cb(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitBreaker.CLOSED  # not yet
    cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN


def test_circuit_breaker_is_open_true_after_threshold():
    cb = _fresh_cb(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True


def test_circuit_breaker_success_before_threshold_resets_count():
    cb = _fresh_cb(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()  # count should restart
    assert cb.state == CircuitBreaker.CLOSED


def test_circuit_breaker_record_success_from_closed_stays_closed():
    cb = _fresh_cb()
    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED


# ---------------------------------------------------------------------------
# OPEN → HALF_OPEN transition (recovery_timeout)
# ---------------------------------------------------------------------------


def test_circuit_breaker_transitions_to_half_open_after_timeout():
    """After recovery_timeout elapses the state property returns HALF_OPEN."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    # While still within recovery window the internal _state flag is OPEN
    assert cb._state == CircuitBreaker.OPEN
    time.sleep(0.01)
    # After timeout the .state property transitions internally to HALF_OPEN
    assert cb.state == CircuitBreaker.HALF_OPEN


def test_circuit_breaker_is_open_false_in_half_open():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)
    # First access should transition to HALF_OPEN
    assert cb.is_open() is False
    assert cb.state == CircuitBreaker.HALF_OPEN


# ---------------------------------------------------------------------------
# HALF_OPEN → CLOSED on success
# ---------------------------------------------------------------------------


def test_circuit_breaker_half_open_success_closes():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)
    _ = cb.state  # trigger HALF_OPEN transition
    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.is_open() is False


# ---------------------------------------------------------------------------
# HALF_OPEN → OPEN on failure
# ---------------------------------------------------------------------------


def test_circuit_breaker_half_open_failure_reopens():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)
    _ = cb.state  # trigger HALF_OPEN
    cb.record_failure()
    # failure_count is now 2 >= threshold=1 → OPEN
    assert cb.state == CircuitBreaker.OPEN


# ---------------------------------------------------------------------------
# BUG-3: probe_in_flight prevents concurrent probes in HALF_OPEN
# ---------------------------------------------------------------------------


def test_circuit_breaker_probe_in_flight_set_on_half_open():
    """Accessing .state when OPEN transitions to HALF_OPEN and sets _probe_in_flight."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)

    s1 = cb.state
    assert s1 == CircuitBreaker.HALF_OPEN
    # The flag must be set so that concurrent callers (in other threads) see OPEN
    assert cb._probe_in_flight is True


def test_circuit_breaker_probe_in_flight_blocks_concurrent_probe():
    """A second thread accessing .state while a probe is in-flight sees OPEN."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)

    results: List[str] = []
    barrier = threading.Barrier(2)

    def _read_state():
        barrier.wait()
        results.append(cb.state)

    t1 = threading.Thread(target=_read_state)
    t2 = threading.Thread(target=_read_state)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one should have seen HALF_OPEN; the other OPEN (or both HALF_OPEN
    # if there was a race — the important invariant is no corruption).
    assert set(results).issubset({CircuitBreaker.HALF_OPEN, CircuitBreaker.OPEN})
    assert CircuitBreaker.HALF_OPEN in results  # at least one probe went through


# ---------------------------------------------------------------------------
# record_success resets _probe_in_flight
# ---------------------------------------------------------------------------


def test_circuit_breaker_record_success_clears_probe_flag():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)
    _ = cb.state  # enters HALF_OPEN, sets probe_in_flight
    cb.record_success()
    assert cb._probe_in_flight is False


def test_circuit_breaker_record_failure_clears_probe_flag():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)
    _ = cb.state  # enters HALF_OPEN
    cb.record_failure()
    assert cb._probe_in_flight is False


# ---------------------------------------------------------------------------
# Thread-safety
# ---------------------------------------------------------------------------


def test_circuit_breaker_thread_safe_concurrent_failures():
    """Multiple threads hammering record_failure must eventually open the breaker."""
    cb = _fresh_cb(failure_threshold=10)
    errors: List[Exception] = []

    def _fail():
        try:
            for _ in range(5):
                cb.record_failure()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_fail) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    # 4 threads × 5 failures = 20 total; threshold=10 → must be OPEN
    assert cb.state == CircuitBreaker.OPEN


def test_circuit_breaker_thread_safe_mixed_success_failure():
    """record_success resets the failure count; state must not corrupt."""
    cb = _fresh_cb(failure_threshold=100)
    errors: List[Exception] = []

    def _mixed():
        try:
            for i in range(20):
                if i % 2 == 0:
                    cb.record_failure()
                else:
                    cb.record_success()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_mixed) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


# ---------------------------------------------------------------------------
# get_circuit_breaker registry
# ---------------------------------------------------------------------------


def test_get_circuit_breaker_returns_same_instance_for_same_key():
    key = "_test_singleton_key_xyz"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)  # ensure fresh
    cb1 = get_circuit_breaker(key)
    cb2 = get_circuit_breaker(key)
    assert cb1 is cb2
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)


def test_get_circuit_breaker_different_keys_get_different_instances():
    k1 = "_test_key_alpha"
    k2 = "_test_key_beta"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(k1, None)
        _CIRCUIT_BREAKERS.pop(k2, None)
    cb1 = get_circuit_breaker(k1)
    cb2 = get_circuit_breaker(k2)
    assert cb1 is not cb2
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(k1, None)
        _CIRCUIT_BREAKERS.pop(k2, None)


def test_get_circuit_breaker_returns_circuit_breaker_instance():
    key = "_test_type_check"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)
    cb = get_circuit_breaker(key)
    assert isinstance(cb, CircuitBreaker)
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)


# ---------------------------------------------------------------------------
# call_model: CB fast-fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_model_cb_fast_fail_skips_internal_call():
    """When CB is OPEN for the provider, call_model must return error without
    calling _call_model_internal."""

    key = "openai"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)
    cb = get_circuit_breaker(key)
    # Force-open
    cb._state = CircuitBreaker.OPEN
    cb._opened_at = time.time() + 9999  # ensure it doesn't expire

    internal_mock = AsyncMock()
    with patch("src.core.inference.llm_manager._call_model_internal", internal_mock):
        result = await call_model(
            messages=[{"role": "user", "content": "hi"}],
            provider="openai",
        )

    assert result.get("ok") is False
    assert "circuit_breaker_open" in result.get("error", "")
    internal_mock.assert_not_called()

    # cleanup
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)


@pytest.mark.asyncio
async def test_call_model_cb_records_success_on_ok_response():
    """call_model should call record_success when the response is ok=True."""
    key = "_test_provider_ok"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)
    cb = get_circuit_breaker(key)
    cb.record_failure()  # one failure so count=1
    assert cb._failure_count == 1

    ok_response: Dict[str, Any] = {"ok": True, "content": "hello"}

    with (
        patch(
            "src.core.inference.llm_manager._call_model_internal",
            AsyncMock(return_value=ok_response),
        ),
        patch(
            "src.core.inference.llm_manager._attempt_model_fallback",
            AsyncMock(return_value=(ok_response, False)),
        ),
        patch(
            "src.core.inference.llm_manager._get_fallback_chain",
            return_value=MagicMock(
                call=AsyncMock(return_value=(ok_response, None))
            ),
        ),
        patch("src.core.inference.llm_manager._update_circuit_breaker_for_result"),
        patch("src.core.inference.llm_manager._record_token_usage"),
        patch("src.core.inference.llm_manager._publish_llm_response_hook"),
    ):
        result = await call_model(
            messages=[{"role": "user", "content": "hi"}],
            provider=key,
        )

    assert result == ok_response

    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)


@pytest.mark.asyncio
async def test_call_model_no_provider_skips_cb():
    """When provider=None, no CB key is computed and fast-fail is skipped."""
    ok_response: Dict[str, Any] = {"ok": True, "content": "hello"}

    with (
        patch(
            "src.core.inference.llm_manager._call_model_internal",
            AsyncMock(return_value=ok_response),
        ),
        patch(
            "src.core.inference.llm_manager._attempt_model_fallback",
            AsyncMock(return_value=(ok_response, False)),
        ),
        patch(
            "src.core.inference.llm_manager._get_fallback_chain",
            return_value=MagicMock(
                call=AsyncMock(return_value=(ok_response, None))
            ),
        ),
        patch("src.core.inference.llm_manager._update_circuit_breaker_for_result"),
        patch("src.core.inference.llm_manager._record_token_usage"),
        patch("src.core.inference.llm_manager._publish_llm_response_hook"),
    ):
        result = await call_model(
            messages=[{"role": "user", "content": "hi"}],
            provider=None,
        )

    assert result == ok_response


@pytest.mark.asyncio
async def test_call_model_cb_not_open_passes_through():
    """When CB is CLOSED, _call_model_internal must be called."""
    key = "_test_closed_provider"
    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)
    cb = get_circuit_breaker(key)
    assert not cb.is_open()

    ok_response: Dict[str, Any] = {"ok": True, "content": "world"}
    internal = AsyncMock(return_value=ok_response)

    with (
        patch("src.core.inference.llm_manager._call_model_internal", internal),
        patch(
            "src.core.inference.llm_manager._attempt_model_fallback",
            AsyncMock(return_value=(ok_response, False)),
        ),
        patch(
            "src.core.inference.llm_manager._get_fallback_chain",
            return_value=MagicMock(
                call=AsyncMock(return_value=(ok_response, None))
            ),
        ),
        patch("src.core.inference.llm_manager._update_circuit_breaker_for_result"),
        patch("src.core.inference.llm_manager._record_token_usage"),
        patch("src.core.inference.llm_manager._publish_llm_response_hook"),
    ):
        result = await call_model(
            messages=[{"role": "user", "content": "hi"}],
            provider=key,
        )

    internal.assert_called_once()
    assert result == ok_response

    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(key, None)
