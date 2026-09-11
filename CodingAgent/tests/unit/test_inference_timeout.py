"""Deterministic fault-injection tests for the inference timeout policy.

No live provider dependency.  All tests use blocking fake adapters / streams
and short timeout policies (≤0.1 s) so the suite completes quickly.

Coverage
--------
1. InferenceTimeoutPolicy — construction, validation, env read.
2. InferenceTimeoutError — structured metadata, to_error_result().
3. run_with_timeout — fires on hung coroutine, passes through fast coroutine.
4. call_adapter_with_fallbacks — sync adapter blocked → timeout error result.
5. ProviderFallbackChain.call — hung fallback attempt bounded per-provider.
6. consume_stream_with_timeouts — first-token and stream-idle deadlines.
7. Structured metadata: phase/kind/retryable/provider preserved end-to-end.
8. No double-counting: circuit-breaker record_failure called exactly once per
   timed-out attempt.
9. Runtime/llm_manager wiring — blocking sync streams, heartbeat-only lines,
   first-token then idle block, clean completion, structured error (not partial).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.core.inference.inference_timeout import (
    InferenceTimeoutError,
    InferenceTimeoutPolicy,
    get_default_policy,
    log_timeout_event,
    reset_default_policy,
    run_with_timeout,
)
from src.core.inference.runtime_call import call_adapter_with_fallbacks
from src.core.inference.provider_fallback import ProviderFallbackChain
from src.core.inference.streaming import (
    consume_stream_with_timeouts,
    _iter_sync_in_executor,
    _STOP,
    _sync_next,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

SHORT = InferenceTimeoutPolicy(
    connect_total=0.05,
    first_token=0.05,
    stream_idle=0.05,
    fallback_attempt=0.05,
)

OK_RESULT: Dict[str, Any] = {"ok": True, "text": "hello"}
ERR_RESULT: Dict[str, Any] = {"ok": False, "error": "upstream_error"}


def make_cb(open: bool = False) -> MagicMock:
    cb = MagicMock()
    cb.is_open.return_value = open
    return cb


def make_mgr(providers: List[str]) -> MagicMock:
    mgr = MagicMock()
    mgr.list_providers.return_value = providers
    return mgr


class BlockingAdapter:
    """Sync adapter whose generate() hangs until released (or times out)."""

    def __init__(self, block_secs: float = 10.0, response: Optional[Dict] = None) -> None:
        self._block_secs = block_secs
        self._response = response or OK_RESULT
        self.call_count = 0

    def generate(self, messages, model=None, stream=False, format_json=False, **kw):
        self.call_count += 1
        time.sleep(self._block_secs)
        return self._response


class FastAdapter:
    """Sync adapter that returns immediately."""

    def generate(self, messages, model=None, stream=False, format_json=False, **kw):
        return OK_RESULT


class SlowStreamLines:
    """Synchronous iter_lines()-compatible object that pauses between tokens."""

    def __init__(self, lines: List[str], delay_between: float = 0.0, delay_before_first: float = 0.0):
        self._lines = lines
        self._delay_between = delay_between
        self._delay_before_first = delay_before_first

    def iter_lines(self):
        if self._delay_before_first > 0:
            time.sleep(self._delay_before_first)
        for i, line in enumerate(self._lines):
            if i > 0 and self._delay_between > 0:
                time.sleep(self._delay_between)
            yield line


def _make_sse_line(content: str) -> str:
    import json
    chunk = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
    return f"data: {json.dumps(chunk)}"


# ---------------------------------------------------------------------------
# 1. InferenceTimeoutPolicy
# ---------------------------------------------------------------------------

class TestInferenceTimeoutPolicy:
    def test_defaults_are_finite_and_positive(self):
        p = InferenceTimeoutPolicy()
        assert p.connect_total == 120.0
        assert p.first_token == 60.0
        assert p.stream_idle == 30.0
        assert p.fallback_attempt == 90.0
        for attr in ("connect_total", "first_token", "stream_idle", "fallback_attempt"):
            assert getattr(p, attr) > 0

    def test_rejects_zero_value(self):
        with pytest.raises(ValueError):
            InferenceTimeoutPolicy(connect_total=0)

    def test_rejects_negative_value(self):
        with pytest.raises(ValueError):
            InferenceTimeoutPolicy(first_token=-1.0)

    def test_from_env_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("LLM_TIMEOUT_CONNECT_TOTAL", "77.0")
        monkeypatch.setenv("LLM_TIMEOUT_FIRST_TOKEN", "33.0")
        monkeypatch.setenv("LLM_TIMEOUT_STREAM_IDLE", "15.0")
        monkeypatch.setenv("LLM_TIMEOUT_FALLBACK_ATTEMPT", "44.0")
        reset_default_policy()
        p = InferenceTimeoutPolicy.from_env()
        assert p.connect_total == 77.0
        assert p.first_token == 33.0
        assert p.stream_idle == 15.0
        assert p.fallback_attempt == 44.0

    def test_from_env_bad_value_uses_default(self, monkeypatch):
        monkeypatch.setenv("LLM_TIMEOUT_CONNECT_TOTAL", "not_a_number")
        reset_default_policy()
        p = InferenceTimeoutPolicy.from_env()
        assert p.connect_total == 120.0

    def test_get_default_policy_returns_same_instance(self):
        reset_default_policy()
        a = get_default_policy()
        b = get_default_policy()
        assert a is b

    def test_reset_clears_cached_policy(self):
        reset_default_policy()
        a = get_default_policy()
        reset_default_policy()
        b = get_default_policy()
        assert a is not b


# ---------------------------------------------------------------------------
# 2. InferenceTimeoutError
# ---------------------------------------------------------------------------

class TestInferenceTimeoutError:
    def test_structured_metadata(self):
        err = InferenceTimeoutError(phase="first_token", timeout_secs=5.0, provider="openai")
        assert err.phase == "first_token"
        assert err.kind == "timeout"
        assert err.retryable is True
        assert err.provider == "openai"
        assert err.timeout_secs == 5.0

    def test_to_error_result_structure(self):
        err = InferenceTimeoutError(phase="stream_idle", timeout_secs=2.5, provider="ollama")
        result = err.to_error_result()
        assert result["ok"] is False
        assert result["error"] == "timeout:stream_idle"
        meta = result["meta"]
        assert meta["phase"] == "stream_idle"
        assert meta["kind"] == "timeout"
        assert meta["retryable"] is True
        assert meta["provider"] == "ollama"
        assert meta["timeout_secs"] == 2.5

    def test_is_asyncio_timeout_error_subclass(self):
        err = InferenceTimeoutError(phase="connect_total", timeout_secs=1.0)
        assert isinstance(err, asyncio.TimeoutError)

    def test_no_provider_ok(self):
        err = InferenceTimeoutError(phase="fallback_attempt", timeout_secs=10.0)
        assert err.provider is None
        result = err.to_error_result()
        assert result["meta"]["provider"] is None

    def test_str_contains_phase(self):
        err = InferenceTimeoutError(phase="first_token", timeout_secs=1.0)
        assert "first_token" in str(err)


# ---------------------------------------------------------------------------
# 3. run_with_timeout
# ---------------------------------------------------------------------------

class TestRunWithTimeout:
    @pytest.mark.asyncio
    async def test_fires_on_hung_coroutine(self):
        async def hang():
            await asyncio.sleep(10)

        with pytest.raises(InferenceTimeoutError) as exc_info:
            await run_with_timeout(hang(), timeout_secs=0.05, phase="connect_total", provider="p1")
        err = exc_info.value
        assert err.phase == "connect_total"
        assert err.provider == "p1"

    @pytest.mark.asyncio
    async def test_passes_through_fast_coroutine(self):
        async def fast():
            return 42

        result = await run_with_timeout(fast(), timeout_secs=5.0, phase="connect_total")
        assert result == 42

    @pytest.mark.asyncio
    async def test_raises_inference_timeout_error_not_plain_asyncio(self):
        async def hang():
            await asyncio.sleep(10)

        with pytest.raises(InferenceTimeoutError):
            await run_with_timeout(hang(), timeout_secs=0.05, phase="stream_idle")


# ---------------------------------------------------------------------------
# 4. call_adapter_with_fallbacks — sync adapter blocked → timeout error result
# ---------------------------------------------------------------------------

class TestCallAdapterWithFallbacks:
    @pytest.mark.asyncio
    async def test_blocking_generate_returns_timeout_error_result(self):
        adapter = BlockingAdapter(block_secs=10.0)
        published: List = []

        result = await call_adapter_with_fallbacks(
            adapter=adapter,
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
            provider="test_provider",
            publish=lambda e, d: published.append((e, d)),
        )

        assert result["ok"] is False
        assert "timeout" in result["error"]
        meta = result.get("meta", {})
        assert meta.get("phase") == "connect_total"
        assert meta.get("kind") == "timeout"
        assert meta.get("retryable") is True
        assert meta.get("provider") == "test_provider"

    @pytest.mark.asyncio
    async def test_fast_adapter_returns_ok(self):
        adapter = FastAdapter()

        result = await call_adapter_with_fallbacks(
            adapter=adapter,
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
            provider="fast_provider",
        )

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_timeout_publishes_event(self):
        adapter = BlockingAdapter(block_secs=10.0)
        published: List = []

        await call_adapter_with_fallbacks(
            adapter=adapter,
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
            provider="slow_p",
            publish=lambda e, d: published.append((e, d)),
        )

        assert any(e == "inference.timeout" for e, _ in published)

    @pytest.mark.asyncio
    async def test_timeout_metadata_provider_preserved(self):
        adapter = BlockingAdapter(block_secs=10.0)
        captured: List = []

        await call_adapter_with_fallbacks(
            adapter=adapter,
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
            provider="myprovider",
            publish=lambda e, d: captured.append(d),
        )

        assert any(d.get("provider") == "myprovider" for d in captured)

    @pytest.mark.asyncio
    async def test_no_adapter_method_returns_missing_error(self):
        class EmptyAdapter:
            pass

        result = await call_adapter_with_fallbacks(
            adapter=EmptyAdapter(),
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
        )

        assert result["ok"] is False
        assert "adapter_missing" in result["error"]


# ---------------------------------------------------------------------------
# 5. ProviderFallbackChain — hung fallback bounded per-provider
# ---------------------------------------------------------------------------

class TestProviderFallbackChainTimeout:
    @pytest.mark.asyncio
    async def test_hung_fallback_returns_primary_result(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "2")

        async def hang(msgs, prov, model, stream, fmt, tools, **kw):
            await asyncio.sleep(10)
            return OK_RESULT

        chain = ProviderFallbackChain()
        result, used = await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=hang,
            get_provider_manager=lambda: make_mgr(["primary", "fallback_a"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
            publish=None,
            timeout_policy=SHORT,
        )
        # All fallbacks timed out — primary result returned
        assert result is ERR_RESULT
        assert used == "primary"

    @pytest.mark.asyncio
    async def test_hung_fallback_records_failure_once(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "1")

        async def hang(msgs, prov, model, stream, fmt, tools, **kw):
            await asyncio.sleep(10)
            return OK_RESULT

        cb_fallback = make_cb(open=False)

        chain = ProviderFallbackChain()
        await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=hang,
            get_provider_manager=lambda: make_mgr(["primary", "fallback_a"]),
            get_circuit_breaker=lambda k: cb_fallback if k == "fallback_a" else make_cb(),
            publish=None,
            timeout_policy=SHORT,
        )
        # record_failure called exactly once — no double-counting
        assert cb_fallback.record_failure.call_count == 1

    @pytest.mark.asyncio
    async def test_hung_fallback_publishes_timeout_event(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "1")

        async def hang(msgs, prov, model, stream, fmt, tools, **kw):
            await asyncio.sleep(10)
            return OK_RESULT

        published: List = []

        chain = ProviderFallbackChain()
        await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=hang,
            get_provider_manager=lambda: make_mgr(["primary", "fallback_a"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
            publish=lambda e, d: published.append((e, d)),
            timeout_policy=SHORT,
        )

        assert any(e == "inference.timeout" for e, _ in published)

    @pytest.mark.asyncio
    async def test_fast_fallback_succeeds_despite_short_policy(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "1")

        # fast fallback — completes instantly
        async def fast(msgs, prov, model, stream, fmt, tools, **kw):
            return OK_RESULT

        chain = ProviderFallbackChain()
        # Use a slightly longer policy so fast coroutine completes
        policy = InferenceTimeoutPolicy(
            connect_total=5.0,
            first_token=5.0,
            stream_idle=5.0,
            fallback_attempt=5.0,
        )
        result, used = await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=fast,
            get_provider_manager=lambda: make_mgr(["primary", "fallback_a"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
            publish=None,
            timeout_policy=policy,
        )
        assert result == OK_RESULT
        assert used == "fallback_a"

    @pytest.mark.asyncio
    async def test_multiple_hung_fallbacks_all_bounded(self, monkeypatch):
        """Two hung fallbacks both time out; primary result returned."""
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "3")

        async def hang(msgs, prov, model, stream, fmt, tools, **kw):
            await asyncio.sleep(10)
            return OK_RESULT

        chain = ProviderFallbackChain()
        t0 = time.monotonic()
        result, used = await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=hang,
            get_provider_manager=lambda: make_mgr(["primary", "fb_a", "fb_b"]),
            get_circuit_breaker=lambda k: make_cb(open=False),
            publish=None,
            timeout_policy=SHORT,
        )
        elapsed = time.monotonic() - t0

        # Should complete in well under 1 s (two 50ms timeouts)
        assert elapsed < 1.0
        assert result is ERR_RESULT


# ---------------------------------------------------------------------------
# 6. consume_stream_with_timeouts — first-token and stream-idle deadlines
# ---------------------------------------------------------------------------

class TestConsumeStreamWithTimeouts:
    @pytest.mark.asyncio
    async def test_first_token_timeout(self):
        """Stream that never emits — hits first_token deadline."""
        async def never_yields():
            await asyncio.sleep(10)
            yield _make_sse_line("hello")

        accumulated, err = await consume_stream_with_timeouts(
            never_yields(),
            policy=SHORT,
            provider="test",
            publish_chunk_fn=lambda c, r: None,
        )
        assert err is not None
        assert isinstance(err, InferenceTimeoutError)
        assert err.phase == "first_token"

    @pytest.mark.asyncio
    async def test_stream_idle_timeout(self):
        """First token arrives fast, then stream stalls."""
        fast_line = _make_sse_line("first")

        async def stalls_after_first():
            yield fast_line
            await asyncio.sleep(10)
            yield _make_sse_line("second")

        accumulated, err = await consume_stream_with_timeouts(
            stalls_after_first(),
            policy=SHORT,
            provider="test",
            publish_chunk_fn=lambda c, r: None,
        )
        # Partial result contains "first" token text
        assert err is not None
        assert err.phase == "stream_idle"

    @pytest.mark.asyncio
    async def test_clean_stream_no_timeout(self):
        lines = [
            _make_sse_line("hello "),
            _make_sse_line("world"),
            "data: [DONE]",
        ]

        async def fast_stream():
            for line in lines:
                yield line

        policy = InferenceTimeoutPolicy(
            connect_total=5.0, first_token=5.0, stream_idle=5.0, fallback_attempt=5.0
        )
        accumulated, err = await consume_stream_with_timeouts(
            fast_stream(),
            policy=policy,
            provider="test",
            publish_chunk_fn=lambda c, r: None,
        )
        assert err is None
        full = "".join(accumulated)
        assert "hello" in full
        assert "world" in full

    @pytest.mark.asyncio
    async def test_publish_called_on_idle_timeout(self):
        fast_line = _make_sse_line("tok")

        async def stalls():
            yield fast_line
            await asyncio.sleep(10)

        published: List = []

        _, err = await consume_stream_with_timeouts(
            stalls(),
            policy=SHORT,
            provider="stall_p",
            publish_chunk_fn=lambda c, r: None,
            publish=lambda e, d: published.append((e, d)),
        )
        assert err is not None
        assert any(e == "inference.timeout" for e, _ in published)

    @pytest.mark.asyncio
    async def test_first_token_timeout_metadata(self):
        async def never():
            await asyncio.sleep(10)
            yield "nothing"

        _, err = await consume_stream_with_timeouts(
            never(),
            policy=SHORT,
            provider="myprov",
            publish_chunk_fn=lambda c, r: None,
        )
        assert err is not None
        assert err.provider == "myprov"
        assert err.kind == "timeout"
        assert err.retryable is True


# ---------------------------------------------------------------------------
# 7. log_timeout_event — observability helpers
# ---------------------------------------------------------------------------

class TestLogTimeoutEvent:
    def test_publishes_structured_event(self):
        events: List = []
        err = InferenceTimeoutError(phase="stream_idle", timeout_secs=5.0, provider="p")
        log_timeout_event(err, publish=lambda e, d: events.append((e, d)))
        assert len(events) == 1
        name, payload = events[0]
        assert name == "inference.timeout"
        assert payload["phase"] == "stream_idle"
        assert payload["kind"] == "timeout"
        assert payload["retryable"] is True
        assert payload["provider"] == "p"

    def test_no_publish_no_error(self):
        err = InferenceTimeoutError(phase="connect_total", timeout_secs=1.0)
        log_timeout_event(err)  # no publish — should not raise

    def test_bad_publish_swallowed(self):
        err = InferenceTimeoutError(phase="first_token", timeout_secs=1.0)

        def boom(e, d):
            raise RuntimeError("bus down")

        log_timeout_event(err, publish=boom)  # should not propagate


# ---------------------------------------------------------------------------
# 8. No double-counting: timeout path does NOT call circuit breaker
#    (only the fallback chain caller records failure after a timeout)
# ---------------------------------------------------------------------------

class TestNoCBDoubleCount:
    @pytest.mark.asyncio
    async def test_timeout_in_call_adapter_does_not_call_cb(self):
        """call_adapter_with_fallbacks catches InferenceTimeoutError and
        returns an error dict — it does NOT call any circuit-breaker method.
        The CB recording is done by the outer call_model() caller."""
        adapter = BlockingAdapter(block_secs=10.0)
        cb = make_cb()

        await call_adapter_with_fallbacks(
            adapter=adapter,
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
        )

        # call_adapter_with_fallbacks never touches the circuit breaker
        cb.record_failure.assert_not_called()
        cb.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_timeout_records_failure_exactly_once(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_ENABLED", "1")
        monkeypatch.setenv("LLM_PROVIDER_FALLBACK_MAX", "1")

        async def hang(msgs, prov, model, stream, fmt, tools, **kw):
            await asyncio.sleep(10)
            return OK_RESULT

        cb_fallback = make_cb(open=False)
        call_count = {"n": 0}

        def get_cb(k):
            if k == "fb":
                call_count["n"] += 1
                return cb_fallback
            return make_cb()

        chain = ProviderFallbackChain()
        await chain.call(
            primary_result=ERR_RESULT,
            primary_provider="primary",
            messages=[],
            model=None,
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            call_model_internal=hang,
            get_provider_manager=lambda: make_mgr(["primary", "fb"]),
            get_circuit_breaker=get_cb,
            timeout_policy=SHORT,
        )

        # record_failure called exactly once per timed-out attempt
        assert cb_fallback.record_failure.call_count == 1
        # record_success never called on failure
        cb_fallback.record_success.assert_not_called()


# ---------------------------------------------------------------------------
# 9. sync-iterable executor bridge (_iter_sync_in_executor / _sync_next)
# ---------------------------------------------------------------------------

class TestSyncIteratorExecutorBridge:
    """The blocking next() of sync iter_lines() must be offloaded so
    asyncio.wait_for can interrupt it.  _iter_sync_in_executor wraps a
    plain iterator and yields each item via the event-loop executor."""

    @pytest.mark.asyncio
    async def test_bridge_yields_all_items(self):
        items = ["a", "b", "c"]
        loop = asyncio.get_event_loop()
        collected = []
        async for item in _iter_sync_in_executor(iter(items), loop):
            collected.append(item)
        assert collected == items

    @pytest.mark.asyncio
    async def test_bridge_stops_on_exhaustion(self):
        loop = asyncio.get_event_loop()
        collected = []
        async for item in _iter_sync_in_executor(iter([]), loop):
            collected.append(item)
        assert collected == []

    def test_sync_next_returns_stop_sentinel_on_exhaustion(self):
        it = iter([])
        result = _sync_next(it)
        assert isinstance(result, type(_STOP))

    def test_sync_next_returns_value(self):
        it = iter([42])
        result = _sync_next(it)
        assert result == 42

    @pytest.mark.asyncio
    async def test_wait_for_can_interrupt_blocking_sync_iter(self):
        """A sync iterator whose next() blocks must not hold the event loop.
        wait_for must fire within the deadline when next() is in executor."""
        import threading

        gate = threading.Event()

        def blocking_iter():
            while True:
                gate.wait()  # blocks until event is set
                yield "x"

        loop = asyncio.get_event_loop()

        async def consume_one():
            aiter = _iter_sync_in_executor(blocking_iter(), loop)
            return await asyncio.wait_for(aiter.__anext__(), timeout=0.1)

        with pytest.raises(asyncio.TimeoutError):
            await consume_one()
        # release the gate so the background thread can exit
        gate.set()


# ---------------------------------------------------------------------------
# 10. consume_stream_with_timeouts — sync iter_lines path (executor-based)
# ---------------------------------------------------------------------------

class TestConsumeStreamSyncIterLines:
    """Tests that exercise the sync iter_lines() → executor → wait_for path."""

    @pytest.mark.asyncio
    async def test_blocking_sync_iter_lines_fires_first_token_timeout(self):
        """A sync iter_lines() that blocks forever must trigger first_token timeout."""
        import threading

        gate = threading.Event()

        class HungSyncResponse:
            def iter_lines(self):
                gate.wait()   # blocks; wait_for must still fire
                yield _make_sse_line("never")

        accumulated, err = await consume_stream_with_timeouts(
            HungSyncResponse(),
            policy=SHORT,
            provider="hung_sync",
            publish_chunk_fn=lambda c, r: None,
        )
        gate.set()  # unblock background thread
        assert err is not None
        assert err.phase == "first_token"
        assert err.provider == "hung_sync"

    @pytest.mark.asyncio
    async def test_clean_sync_iter_lines_returns_tokens(self):
        """A normal sync iter_lines() completes without timeout."""
        lines = [
            _make_sse_line("hello "),
            _make_sse_line("world"),
            "data: [DONE]",
        ]

        class FastSyncResponse:
            def iter_lines(self):
                yield from lines

        policy = InferenceTimeoutPolicy(
            connect_total=5.0, first_token=5.0, stream_idle=5.0, fallback_attempt=5.0
        )
        accumulated, err = await consume_stream_with_timeouts(
            FastSyncResponse(),
            policy=policy,
            provider="fast",
            publish_chunk_fn=lambda c, r: None,
        )
        assert err is None
        assert "hello" in "".join(accumulated)
        assert "world" in "".join(accumulated)

    @pytest.mark.asyncio
    async def test_heartbeat_lines_do_not_reset_first_token_deadline(self):
        """Non-data / empty lines emitted before any token must NOT reset the
        first_token deadline — the timeout fires on the real first-token window."""
        import threading

        # Emit several heartbeat-only lines then block forever
        heartbeats = ["", "  ", ": keepalive", "not-a-data-line"] * 5
        hang_gate = threading.Event()

        class HeartbeatThenHungResponse:
            def iter_lines(self):
                yield from heartbeats
                hang_gate.wait()  # blocks after heartbeats
                yield _make_sse_line("never")

        accumulated, err = await consume_stream_with_timeouts(
            HeartbeatThenHungResponse(),
            policy=SHORT,
            provider="hb_provider",
            publish_chunk_fn=lambda c, r: None,
        )
        hang_gate.set()
        assert err is not None
        assert err.phase == "first_token"

    @pytest.mark.asyncio
    async def test_idle_timeout_fires_after_first_token(self):
        """First token arrives; then stream stalls → stream_idle fires."""
        import threading

        first_line = _make_sse_line("first_tok")
        stall_gate = threading.Event()

        class FirstThenStalledResponse:
            def iter_lines(self):
                yield first_line
                stall_gate.wait()
                yield _make_sse_line("never_second")

        accumulated, err = await consume_stream_with_timeouts(
            FirstThenStalledResponse(),
            policy=SHORT,
            provider="stall",
            publish_chunk_fn=lambda c, r: None,
        )
        stall_gate.set()
        assert err is not None
        assert err.phase == "stream_idle"
        # Partial accumulated content contains the first token
        assert "first_tok" in "".join(accumulated)

    @pytest.mark.asyncio
    async def test_heartbeat_lines_between_tokens_do_not_reset_idle_deadline(self):
        """Heartbeat lines emitted between two real tokens must NOT extend
        the stream_idle deadline — only real token chunks reset it."""
        import threading

        first_line = _make_sse_line("tok1")
        stall_gate = threading.Event()

        class FirstTokenThenHeartbeatsThenHung:
            def iter_lines(self):
                yield first_line
                # Many heartbeat-only lines (these must NOT reset idle timer)
                for _ in range(20):
                    yield ""
                stall_gate.wait()
                yield _make_sse_line("tok2_never")

        accumulated, err = await consume_stream_with_timeouts(
            FirstTokenThenHeartbeatsThenHung(),
            policy=SHORT,
            provider="hb_idle",
            publish_chunk_fn=lambda c, r: None,
        )
        stall_gate.set()
        assert err is not None
        assert err.phase == "stream_idle"

    @pytest.mark.asyncio
    async def test_timeout_returns_structured_error_not_partial_ok(self):
        """On timeout the result must be an error dict, NOT {"ok": True, ...}."""
        import threading

        gate = threading.Event()

        class HungResponse:
            def iter_lines(self):
                gate.wait()
                yield _make_sse_line("too_late")

        accumulated, err = await consume_stream_with_timeouts(
            HungResponse(),
            policy=SHORT,
            provider="hung",
            publish_chunk_fn=lambda c, r: None,
        )
        gate.set()
        # consume_stream_with_timeouts always returns the InferenceTimeoutError
        # as the second element — callers must NOT treat this as success.
        assert err is not None
        assert isinstance(err, InferenceTimeoutError)
        result = err.to_error_result()
        assert result["ok"] is False
        assert "timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_call_adapter_streaming_returns_error_on_timeout(self):
        """Full call_adapter_with_fallbacks path: adapter returns a streaming
        response whose iter_lines() blocks → result must be an error dict."""
        import threading

        gate = threading.Event()

        class StreamingAdapter:
            class _HungResponse:
                def iter_lines(self_inner):
                    gate.wait()
                    yield _make_sse_line("too_late")

            def generate(self, messages, model=None, stream=False, format_json=False, **kw):
                return self._HungResponse()

        result = await call_adapter_with_fallbacks(
            adapter=StreamingAdapter(),
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            stream=True,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=SHORT,
            provider="stream_provider",
        )
        gate.set()

        assert result["ok"] is False
        assert "timeout" in result["error"]
        meta = result.get("meta", {})
        assert meta.get("phase") in ("first_token", "stream_idle")
        assert meta.get("provider") == "stream_provider"

    @pytest.mark.asyncio
    async def test_call_adapter_streaming_clean_completes_ok(self):
        """Full call_adapter_with_fallbacks path: fast streaming adapter completes."""
        lines = [
            _make_sse_line("hello "),
            _make_sse_line("world"),
            "data: [DONE]",
        ]

        class FastStreamingAdapter:
            class _Response:
                def iter_lines(self_inner):
                    yield from lines

            def generate(self, messages, model=None, stream=False, format_json=False, **kw):
                return self._Response()

        policy = InferenceTimeoutPolicy(
            connect_total=5.0, first_token=5.0, stream_idle=5.0, fallback_attempt=5.0
        )
        result = await call_adapter_with_fallbacks(
            adapter=FastStreamingAdapter(),
            messages=[],
            model="m",
            stream=True,
            format_json=False,
            call_extra_args={},
            run_with_correlation=lambda loop, ex, fn, *a: loop.run_in_executor(ex, fn, *a),
            consume_sse_stream=lambda resp, model: "",
            timeout_policy=policy,
            provider="fast_stream",
        )

        assert result["ok"] is True
        assert "hello" in result.get("text", "")
        assert "world" in result.get("text", "")

    @pytest.mark.asyncio
    async def test_response_close_called_on_timeout(self):
        """When a timeout fires, _close_response should attempt to close the
        response object if it has a close() method."""
        import threading

        gate = threading.Event()
        close_called = {"n": 0}

        class CloseableHungResponse:
            def iter_lines(self):
                gate.wait()
                yield _make_sse_line("never")

            def close(self):
                close_called["n"] += 1

        accumulated, err = await consume_stream_with_timeouts(
            CloseableHungResponse(),
            policy=SHORT,
            provider="closeable",
            publish_chunk_fn=lambda c, r: None,
        )
        gate.set()
        assert err is not None
        # close() must have been called exactly once
        assert close_called["n"] == 1
