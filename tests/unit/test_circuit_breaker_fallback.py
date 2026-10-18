"""Unit tests for circuit breaker fallback and model resolution (Audit Section 6 / Phase 2)."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.inference.llm_manager import (
    _CIRCUIT_BREAKERS,
    _CB_LOCK,
    CircuitBreaker,
    get_circuit_breaker,
    call_model,
)
from src.core.inference.provider_fallback import ProviderFallbackChain


@pytest.mark.asyncio
async def test_circuit_breaker_open_routes_to_secondary_fallback():
    """When primary provider's circuit breaker is open, call_model must invoke fallback chain and succeed via secondary provider."""
    primary_key = "_cb_test_primary"
    secondary_key = "_cb_test_secondary"

    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(primary_key, None)
        _CIRCUIT_BREAKERS.pop(secondary_key, None)

    # Force open the primary breaker
    primary_cb = get_circuit_breaker(primary_key)
    primary_cb._state = CircuitBreaker.OPEN
    primary_cb._opened_at = time.time() + 9999

    primary_mock = AsyncMock()
    fallback_result = {"ok": True, "content": "from secondary provider"}

    # Mock fallback chain that succeeds on secondary
    class _MockFallbackChain:
        async def call(self, *, primary_result, primary_provider, **kwargs):
            assert primary_result.get("ok") is False
            assert "circuit_breaker_open" in primary_result.get("error", "")
            return fallback_result, secondary_key

    try:
        with (
            patch("src.core.inference.llm_manager._call_model_internal", primary_mock),
            patch("src.core.inference.llm_manager._get_fallback_chain", return_value=_MockFallbackChain()),
            patch("src.core.inference.llm_manager._record_token_usage"),
            patch("src.core.inference.llm_manager._publish_llm_response_hook"),
        ):
            res = await call_model(
                messages=[{"role": "user", "content": "hello"}],
                provider=primary_key,
            )

        assert res == fallback_result
        # The primary provider internal call must never be invoked while breaker is open
        primary_mock.assert_not_called()
    finally:
        with _CB_LOCK:
            _CIRCUIT_BREAKERS.pop(primary_key, None)
            _CIRCUIT_BREAKERS.pop(secondary_key, None)


def test_candidate_model_resolution_picks_adapter_default_when_unsupported():
    """Fallback model resolution must pick candidate's default_model when original is unsupported."""
    chain = ProviderFallbackChain()

    candidate_adapter = MagicMock()
    candidate_adapter.models = ["llama3:8b", "mistral:7b"]
    candidate_adapter.default_model = "llama3:8b"

    mgr = MagicMock()
    mgr.get_provider.return_value = candidate_adapter

    # Passing an unsupported OpenAI model ID to an Ollama candidate
    resolved = chain._resolve_candidate_model(
        candidate_key="ollama",
        original_model="gpt-4o",
        mgr=mgr,
    )
    assert resolved == "llama3:8b"


def test_candidate_model_resolution_preserves_supported_model():
    """Fallback model resolution must preserve original model if candidate supports it."""
    chain = ProviderFallbackChain()

    candidate_adapter = MagicMock()
    candidate_adapter.models = ["gpt-4o", "gpt-4o-mini"]
    candidate_adapter.default_model = "gpt-4o-mini"

    mgr = MagicMock()
    mgr.get_provider.return_value = candidate_adapter

    resolved = chain._resolve_candidate_model(
        candidate_key="azure_openai",
        original_model="gpt-4o",
        mgr=mgr,
    )
    assert resolved == "gpt-4o"


@pytest.mark.asyncio
async def test_primary_failure_count_not_reset_by_secondary_fallback_success():
    """Primary provider failures must open primary breaker even when secondary fallback succeeds."""
    primary_key = "_test_primary_fail"
    secondary_key = "_test_secondary_ok"

    with _CB_LOCK:
        _CIRCUIT_BREAKERS.pop(primary_key, None)
        _CIRCUIT_BREAKERS.pop(secondary_key, None)

    primary_cb = get_circuit_breaker(primary_key)
    primary_cb.failure_threshold = 3

    class _MockFallbackChain:
        async def call(self, *, primary_result, primary_provider, **kwargs):
            return {"ok": True, "content": "recovered"}, secondary_key

    try:
        with (
            patch(
                "src.core.inference.llm_manager._call_model_internal",
                AsyncMock(return_value={"ok": False, "error": "http_500"}),
            ),
            patch(
                "src.core.inference.llm_manager._get_fallback_chain",
                return_value=_MockFallbackChain(),
            ),
            patch("src.core.inference.llm_manager._record_token_usage"),
            patch("src.core.inference.llm_manager._publish_llm_response_hook"),
        ):
            for _ in range(3):
                res = await call_model(
                    messages=[{"role": "user", "content": "hi"}],
                    provider=primary_key,
                )
                assert res["ok"] is True

        assert primary_cb._failure_count == 3
        assert primary_cb.is_open() is True
    finally:
        with _CB_LOCK:
            _CIRCUIT_BREAKERS.pop(primary_key, None)
            _CIRCUIT_BREAKERS.pop(secondary_key, None)


def test_circuit_breaker_half_open_single_probe_admission():
    """CircuitBreaker in HALF_OPEN admits exactly one probe and rejects concurrent requests."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.001)
    cb.record_failure()
    time.sleep(0.01)

    assert cb.allow_request() is True
    assert cb.allow_request() is False
    assert cb.state == CircuitBreaker.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.allow_request() is True

