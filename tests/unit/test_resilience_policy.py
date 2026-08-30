"""PERF-02: unified provider resilience policy fault-injection tests.

Contract tests for ``src/core/utils/retry.py`` (centralized retryable-status
classifier, shared phase timeouts, capped+jittered backoff) and its adoption in
the adapters.

Scenario coverage (roadmap acceptance): connection refusal, model warm-up,
timeout, 429, 5xx, stream interruption, retry exhaustion (which leaves the
final error visible for provider fallback to act on).
"""

import asyncio

import pytest

from src.core.inference.adapters.ollama_adapter import (
    OllamaAdapter as _RealOllamaAdapter,
)
from src.core.inference.adapters.openai_compat_adapter import (
    OpenAICompatibleAdapter as _RealOpenAICompatAdapter,
)
from src.core.utils import retry


class TestRetryableClassifier:
    """Central status/exception classification is a single, correct source."""

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_retryable_status_codes(self, code):
        assert retry.is_retryable_status_code(code) is True

    @pytest.mark.parametrize("code", [200, 201, 400, 404, 422, 501, 505, None])
    def test_non_retryable_status_codes(self, code):
        assert retry.is_retryable_status_code(code) is False

    def test_shared_set_is_forwarded_to_adapters(self):
        # The single source of truth is the canonical retryable set.
        assert retry.RETRYABLE_STATUS_CODES == {429, 500, 502, 503, 504}

    def test_connection_error_is_retryable(self):
        assert retry.is_retryable_exception(ConnectionError("refused")) is True

    def test_timeout_is_retryable(self):
        assert retry.is_retryable_exception(TimeoutError("timed out")) is True

    def test_http_error_with_retryable_code(self):
        class _Exc(Exception):
            def __init__(self, code):
                self.response = _Resp(code)

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        assert retry.is_retryable_exception(_Exc(429)) is True
        assert retry.is_retryable_exception(_Exc(504)) is True
        assert retry.is_retryable_exception(_Exc(400)) is False
        assert retry.is_retryable_exception(_Exc(404)) is False


class TestJitteredBackoffCapped:
    def test_delay_is_capped_at_max(self):
        # High attempt with capped delay stays within [max_delay, max_delay + jitter].
        for _ in range(200):
            d = retry.jittered_backoff(20, base_delay=4.0, max_delay=30.0)
            assert d <= 30.0 * 1.5 + 1e-9
            assert d >= 30.0

    def test_delay_grows_then_caps(self):
        d1 = retry.jittered_backoff(1, base_delay=1.0, max_delay=60.0)
        d3 = retry.jittered_backoff(3, base_delay=1.0, max_delay=60.0)
        assert d1 < d3  # exponential growth before cap


class TestAsyncRetryScenarios:
    """Fault-injection scenarios against the centralized async_retry."""

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _HttpExc(Exception):
        def __init__(self, status_code):
            super().__init__(f"status {status_code}")
            self.response = type("_R", (), {"status_code": status_code})()

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        """Replace asyncio.sleep with a no-op so backoff doesn't stall tests."""
        sleeps = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(retry.asyncio, "sleep", _fake_sleep)
        return sleeps

    @pytest.mark.asyncio
    async def test_connection_refusal_retries_then_succeeds(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionError("refused")
            return "ok"

        assert await hit() == "ok"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_model_warm_up_connection_refusal_then_succeeds(self):
        # Local model is still loading into VRAM; first calls hit connection
        # refusal, later call succeeds.
        calls = {"n": 0}

        @retry.async_retry(max_attempts=4, backoff=(0.001, 0.002, 0.004))
        async def warmup():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ConnectionError("target machine refused")
            return "loaded"

        assert await warmup() == "loaded"
        assert calls["n"] == 4

    @pytest.mark.asyncio
    async def test_timeout_retries_then_succeeds(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimeoutError("read timed out")
            return "ok"

        assert await hit() == "ok"

    @pytest.mark.asyncio
    async def test_429_retry_then_succeeds(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            if calls["n"] < 2:
                raise self._HttpExc(429)
            return "ok"

        assert await hit() == "ok"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_5xx_retry_then_succeeds(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            if calls["n"] < 2:
                raise self._HttpExc(503)
            return "ok"

        assert await hit() == "ok"

    @pytest.mark.asyncio
    async def test_non_retryable_status_raises_immediately(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            raise self._HttpExc(400)

        with pytest.raises(Exception) as exc:
            await hit()
        assert "status 400" in str(exc.value)
        assert calls["n"] == 1  # no retry on non-retryable status

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises_final_error(self):
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def hit():
            calls["n"] += 1
            raise ConnectionError("always down")

        with pytest.raises(ConnectionError):
            await hit()
        assert calls["n"] == 3  # final error is surfaced, not swallowed

    @pytest.mark.asyncio
    async def test_stream_interruption_timeout_retries(self):
        # A TimeoutError mid-stream is transient and retried under the policy.
        calls = {"n": 0}

        @retry.async_retry(max_attempts=3, backoff=(0.001, 0.002))
        async def stream():
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("stream idle timeout")
            return {"kind": "chunk"}

        assert await stream() == {"kind": "chunk"}
        assert calls["n"] == 2


class TestAdapterAdoption:
    """OpenAI-compat and Ollama adapters now use the shared classifier."""

    def test_openai_compat_retry_uses_shared_classifier(self, monkeypatch):
        from src.core.inference.adapters import openai_compat_adapter as mod

        import requests

        adapter = _RealOpenAICompatAdapter.__new__(_RealOpenAICompatAdapter)
        adapter.DEFAULT_TIMEOUT = 1.0
        adapter.api_key = None
        adapter.base_url = "http://x"

        calls = {"n": 0}

        def fake_safe_post(ep, headers, payload, timeout, stream):
            calls["n"] += 1
            resp = requests.Response()
            resp.status_code = 429 if calls["n"] == 1 else 200
            return resp

        monkeypatch.setattr(adapter, "_safe_post", fake_safe_post)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        result = adapter._execute_with_retry("ep", {}, stream=False)
        assert result.status_code == 200
        assert calls["n"] == 2

    def test_openai_compat_no_retry_on_400(self, monkeypatch):
        from src.core.inference.adapters import openai_compat_adapter as mod

        import requests

        adapter = _RealOpenAICompatAdapter.__new__(_RealOpenAICompatAdapter)
        adapter.DEFAULT_TIMEOUT = 1.0
        adapter.api_key = None
        adapter.base_url = "http://x"
        calls = {"n": 0}

        def fake_safe_post(ep, headers, payload, timeout, stream):
            calls["n"] += 1
            resp = requests.Response()
            resp.status_code = 400
            return resp

        monkeypatch.setattr(adapter, "_safe_post", fake_safe_post)

        result = adapter._execute_with_retry("ep", {}, stream=False)
        assert result.status_code == 400
        assert calls["n"] == 1  # no retry on non-retryable status

    def test_ollama_retries_429_as_retryable(self, monkeypatch):
        from src.core.inference.adapters import ollama_adapter as mod

        import requests

        adapter = _RealOllamaAdapter.__new__(_RealOllamaAdapter)
        calls = {"n": 0}

        def fake_request(url, timeout, **kwargs):
            calls["n"] += 1
            resp = requests.Response()
            resp.status_code = 429 if calls["n"] == 1 else 200
            return resp

        monkeypatch.setattr(mod.requests, "get", fake_request)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        result = adapter._request_with_retry("get", "http://x")
        assert result.status_code == 200
        assert calls["n"] == 2
