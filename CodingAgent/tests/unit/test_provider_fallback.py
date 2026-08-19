"""Tests for src.core.inference.provider_fallback (P1-1 cross-provider fallback)."""
from __future__ import annotations

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.inference.provider_fallback import ProviderFallbackChain, get_fallback_chain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ERROR_RESULT = {"ok": False, "error": "upstream_error"}
OK_RESULT = {"ok": True, "text": "hello"}


def make_cb(open: bool = False) -> MagicMock:
    cb = MagicMock()
    cb.is_open.return_value = open
    return cb


def make_mgr(providers: list[str]) -> MagicMock:
    mgr = MagicMock()
    mgr.list_providers.return_value = providers
    return mgr


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

class TestNoFallbackNeeded:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_returns_primary_when_result_is_ok(self):
        result, provider = await self.chain.call(
            primary_result=OK_RESULT,
            primary_provider="openai",
            messages=[],
            model="gpt-4o",
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=AsyncMock(),
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(),
        )
        assert result == OK_RESULT
        assert provider == "openai"

    @pytest.mark.asyncio
    async def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "0")
        call_internal = AsyncMock()
        result, provider = await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=call_internal,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(),
        )
        assert result == ERROR_RESULT
        call_internal.assert_not_called()


# ---------------------------------------------------------------------------
# Successful fallback
# ---------------------------------------------------------------------------

class TestSuccessfulFallback:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_uses_first_healthy_candidate(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        call_internal = AsyncMock(return_value=OK_RESULT)
        result, used = await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=call_internal,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
        )
        assert result == OK_RESULT
        assert used == "anthropic"
        call_internal.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_primary_in_candidates(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        called_providers: list[str] = []

        async def fake_internal(msgs, prov, model, stream, fmt, tools, **kw):
            called_providers.append(prov)
            return OK_RESULT

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=fake_internal,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
        )
        assert "openai" not in called_providers

    @pytest.mark.asyncio
    async def test_publishes_event_on_success(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        published: list = []

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model="m",
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=AsyncMock(return_value=OK_RESULT),
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
            publish=lambda event, data: published.append((event, data)),
        )
        assert any(e == "provider.fallback.used" for e, _ in published)

    @pytest.mark.asyncio
    async def test_records_circuit_breaker_success(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        cbs: dict[str, MagicMock] = {
            "openai": make_cb(open=False),
            "anthropic": make_cb(open=False),
        }

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=AsyncMock(return_value=OK_RESULT),
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: cbs.get(k, make_cb()),
        )
        cbs["anthropic"].record_success.assert_called_once()


# ---------------------------------------------------------------------------
# Circuit-breaker skipping
# ---------------------------------------------------------------------------

class TestCircuitBreakerSkipping:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_skips_open_candidates(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "3")

        cbs = {
            "openai": make_cb(open=False),
            "anthropic": make_cb(open=True),   # open — should be skipped
            "cohere": make_cb(open=False),
        }
        called: list[str] = []

        async def fake_internal(msgs, prov, model, stream, fmt, tools, **kw):
            called.append(prov)
            return OK_RESULT

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=fake_internal,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic", "cohere"]),
            get_circuit_breaker=lambda k: cbs.get(k, make_cb()),
        )
        assert "anthropic" not in called
        assert "cohere" in called


# ---------------------------------------------------------------------------
# All candidates fail
# ---------------------------------------------------------------------------

class TestAllCandidatesFail:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_returns_primary_result_when_all_fail(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        result, used = await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=AsyncMock(return_value=ERROR_RESULT),
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
        )
        assert result == ERROR_RESULT
        assert used == "openai"

    @pytest.mark.asyncio
    async def test_records_failure_for_each_failed_candidate(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "2")
        cbs = {
            "openai": make_cb(open=False),
            "anthropic": make_cb(open=False),
            "cohere": make_cb(open=False),
        }

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=AsyncMock(return_value=ERROR_RESULT),
            get_provider_manager=lambda: make_mgr(["openai", "anthropic", "cohere"]),
            get_circuit_breaker=lambda k: cbs.get(k, make_cb()),
        )
        cbs["anthropic"].record_failure.assert_called()

    @pytest.mark.asyncio
    async def test_exception_in_candidate_records_failure(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        cb_anthropic = make_cb(open=False)

        async def boom(msgs, prov, model, stream, fmt, tools, **kw):
            raise RuntimeError("network error")

        result, used = await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=boom,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic"]),
            get_circuit_breaker=lambda k: cb_anthropic if k == "anthropic" else make_cb(),
        )
        assert result == ERROR_RESULT
        cb_anthropic.record_failure.assert_called()


# ---------------------------------------------------------------------------
# Max attempts cap
# ---------------------------------------------------------------------------

class TestMaxAttempts:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_respects_max_fallback_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "1")
        called: list[str] = []

        async def fake(msgs, prov, model, stream, fmt, tools, **kw):
            called.append(prov)
            return ERROR_RESULT

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=fake,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic", "cohere"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
        )
        assert len(called) == 1


# ---------------------------------------------------------------------------
# Custom order via env
# ---------------------------------------------------------------------------

class TestCustomOrder:
    def setup_method(self):
        self.chain = ProviderFallbackChain()

    @pytest.mark.asyncio
    async def test_custom_order_respected(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ORDER", "cohere,anthropic")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "3")
        called: list[str] = []

        async def fake(msgs, prov, model, stream, fmt, tools, **kw):
            called.append(prov)
            return OK_RESULT

        await self.chain.call(
            primary_result=ERROR_RESULT,
            primary_provider="openai",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=fake,
            get_provider_manager=lambda: make_mgr(["openai", "anthropic", "cohere"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
        )
        assert called[0] == "cohere"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_fallback_chain_returns_same_instance(self):
        a = get_fallback_chain()
        b = get_fallback_chain()
        assert a is b

    def test_get_fallback_chain_is_provider_fallback_chain(self):
        assert isinstance(get_fallback_chain(), ProviderFallbackChain)
