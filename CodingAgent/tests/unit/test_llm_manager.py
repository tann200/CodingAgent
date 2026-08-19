import asyncio
import concurrent.futures
import json
import threading
import time
import pytest
import src.core.inference.llm_manager as llm_manager_module
from src.core.inference.llm_manager import (
    ProviderManager,
    _provider_manager,
    get_structured_llm,
    CircuitBreaker,
    get_circuit_breaker,
    _CIRCUIT_BREAKERS,
    _consume_sse_stream,
)
from src.core.orchestration.event_bus import EventBus
from src.core.user_prefs import UserPrefs


def test_provider_manager_initialize_and_list(tmp_path):
    # create a providers_sample.json in tmp_path
    sample = {
        "name": "ollama",
        "base_url": "http://localhost:11434/api",
        "type": "ollama",
        "models": [{"name": "qwen3.5:9b"}],
    }
    pfile = tmp_path / "providers_sample.json"
    pfile.write_text(json.dumps(sample), encoding="utf-8")

    pm = ProviderManager(providers_config_path=str(pfile))
    # ensure no exception
    asyncio.run(pm.initialize())
    lst = pm.list_providers()
    assert isinstance(lst, list)
    assert "ollama" in lst


from unittest.mock import patch, MagicMock  # noqa: E402


def test_get_structured_llm_missing_model_emits_event(monkeypatch, tmp_path):
    # create providers sample in tmp_path
    sample = {
        "name": "ollama",
        "base_url": "http://localhost:11434/api",
        "type": "ollama",
        "models": [{"name": "qwen3.5:9b"}],
    }
    pfile = tmp_path / "providers_sample.json"
    pfile.write_text(json.dumps(sample), encoding="utf-8")

    # configure global provider manager to use this file
    pm = _provider_manager
    _orig_providers = dict(pm._providers)
    _orig_initialized = pm._initialized
    _orig_config_path = pm.providers_config_path
    pm.providers_config_path = str(pfile)
    # reset state so initialize reloads
    pm._initialized = False
    pm._providers = {}

    try:
        # set event bus and capture events
        bus = EventBus()
        events = []
        bus.subscribe("provider.model.missing", lambda payload: events.append(payload))
        pm.set_event_bus(bus)

        # ensure initialized
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            # LM Studio probe responses return full ids; keep consistent
            mock_response.json.return_value = {"models": [{"name": "qwen/qwen3.5-9b"}]}
            mock_get.return_value = mock_response
            asyncio.run(pm.initialize())

        # write prefs that request a missing model
        prefs_path = tmp_path / "prefs.json"
        prefs_data = {
            "selected_model_provider": "ollama",
            "selected_model_name": "nonexistent:1",
        }
        prefs_path.write_text(json.dumps(prefs_data), encoding="utf-8")

        # monkeypatch UserPrefs.load to return our prefs (avoid recursion)
        def _load(path=None):
            return UserPrefs(data=prefs_data, path=prefs_path)

        monkeypatch.setattr(
            "src.core.inference.llm_manager.UserPrefs.load", staticmethod(_load)
        )

        # call get_structured_llm
        client, resolved = asyncio.run(get_structured_llm())
        assert client is not None
        # resolved should be None because model missing
        assert resolved is None
        assert len(events) >= 1
    finally:
        pm._providers = _orig_providers
        pm._initialized = _orig_initialized
        pm.providers_config_path = _orig_config_path


def test_provider_manager_validate_provider_uses_extracted_probe_helper():
    class _AsyncProvider:
        async def validate_connection(self):
            return True

    pm = ProviderManager()
    pm._providers["ollama"] = _AsyncProvider()

    assert asyncio.run(pm.validate_provider("ollama")) is True


def test_provider_manager_initialize_serializes_concurrent_callers(
    tmp_path, monkeypatch
):
    providers_path = tmp_path / "providers.json"
    providers_path.write_text("[]", encoding="utf-8")

    pm = ProviderManager(providers_config_path=str(providers_path))
    start_event = threading.Event()
    calls = {"load": 0, "probe": 0}

    monkeypatch.setattr(
        llm_manager_module,
        "_load_provider_entries",
        lambda raw: [],
    )

    def _fake_load_registered_providers(**kwargs):
        calls["load"] += 1
        time.sleep(0.05)

    def _fake_run_provider_probe_cycle(**kwargs):
        calls["probe"] += 1

    monkeypatch.setattr(
        llm_manager_module,
        "_load_registered_providers",
        _fake_load_registered_providers,
    )
    monkeypatch.setattr(
        llm_manager_module,
        "_run_provider_probe_cycle",
        _fake_run_provider_probe_cycle,
    )

    def _worker() -> None:
        start_event.wait(timeout=1.0)
        asyncio.run(pm.initialize())

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_worker), executor.submit(_worker)]
        start_event.set()
        for future in futures:
            future.result(timeout=2.0)

    assert pm._initialized is True
    assert calls == {"load": 1, "probe": 1}


def test_ensure_provider_manager_initialized_sync_reuses_inflight_task(monkeypatch):
    calls = {"count": 0}
    original_initialized = _provider_manager._initialized

    async def _run_test():
        release_event = asyncio.Event()

        async def _fake_initialize():
            calls["count"] += 1
            await release_event.wait()
            _provider_manager._initialized = True

        monkeypatch.setattr(_provider_manager, "_initialized", False)
        monkeypatch.setattr(llm_manager_module, "_INIT_TASK", None)
        monkeypatch.setattr(_provider_manager, "initialize", _fake_initialize)

        llm_manager_module._ensure_provider_manager_initialized_sync()
        first_task = llm_manager_module._INIT_TASK
        assert first_task is not None

        llm_manager_module._ensure_provider_manager_initialized_sync()
        assert llm_manager_module._INIT_TASK is first_task

        release_event.set()
        await first_task
        await asyncio.sleep(0)

    try:
        asyncio.run(_run_test())
    finally:
        _provider_manager._initialized = original_initialized
        llm_manager_module._INIT_TASK = None

    assert calls["count"] == 1
    assert llm_manager_module._INIT_TASK is None


# ---------------------------------------------------------------------------
# #31: CircuitBreaker tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def setup_method(self):
        # Always start with a fresh breaker to avoid state leaking between tests
        _CIRCUIT_BREAKERS.clear()

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open() is False

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED

    def test_threshold_failures_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_open() is True

    def test_success_resets_to_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open() is True
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open() is False

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True
        time.sleep(0.02)
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.is_open() is False

    def test_get_circuit_breaker_same_instance(self):
        cb1 = get_circuit_breaker("ollama")
        cb2 = get_circuit_breaker("ollama")
        assert cb1 is cb2

    def test_get_circuit_breaker_different_providers(self):
        cb_ollama = get_circuit_breaker("ollama")
        cb_lm = get_circuit_breaker("lm_studio")
        assert cb_ollama is not cb_lm

    @pytest.mark.real_llm
    def test_call_model_fast_fails_when_open(self):
        """call_model must return an error dict immediately when CB is open."""
        import asyncio
        from src.core.inference.llm_manager import call_model

        cb = get_circuit_breaker("ollama")
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open()

        async def _run():
            return await call_model(
                [{"role": "user", "content": "hi"}],
                provider="ollama",
                model="llama3:8b",
            )

        res = asyncio.run(_run())
        assert isinstance(res, dict)
        assert res.get("ok") is False
        assert "circuit_breaker_open" in (res.get("error") or "")

    def test_record_success_clears_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 0
        assert cb.state == CircuitBreaker.CLOSED


def test_consume_sse_stream_publishes_llm_token_alias(monkeypatch):
    class FakeResponse:
        def iter_lines(self):
            yield b'data: {"choices": [{"delta": {"content": "Hel"}}]}'
            yield b'data: {"choices": [{"delta": {"content": "lo"}}]}'
            yield b"data: [DONE]"

    bus = EventBus()
    published = []

    for event_name in ("model.token", "llm.token", "response.stream_end"):

        def make_handler(en):
            return lambda payload: published.append((en, payload))

        bus.subscribe(event_name, make_handler(event_name))

    monkeypatch.setattr(
        "src.core.orchestration.event_bus.get_event_bus",
        lambda: bus,
        raising=False,
    )

    result = _consume_sse_stream(FakeResponse(), model="test-model")

    assert result == "Hello"
    llm_events = [p for e, p in published if e == "llm.token"]
    model_events = [p for e, p in published if e == "model.token"]
    assert llm_events
    assert model_events
    assert any(
        ev.get("partial") is True and ev.get("text") == "Hel" for ev in llm_events
    )
    assert any(
        ev.get("partial") is False and ev.get("full") == "Hello" for ev in llm_events
    )


def test_consume_sse_stream_splits_think_blocks_into_reasoning_events(monkeypatch):
    class FakeResponse:
        def iter_lines(self):
            yield b'data: {"choices": [{"delta": {"content": "Hi<think>plan"}}]}'
            yield b'data: {"choices": [{"delta": {"content": "ning</think> there"}}]}'
            yield b"data: [DONE]"

    bus = EventBus()
    published = []

    for event_name in (
        "response.stream_chunk",
        "llm.token",
        "model.token",
        "response.stream_end",
    ):

        def make_handler(en):
            return lambda payload: published.append((en, payload))

        bus.subscribe(event_name, make_handler(event_name))

    monkeypatch.setattr(
        "src.core.orchestration.event_bus.get_event_bus",
        lambda: bus,
        raising=False,
    )

    result = _consume_sse_stream(FakeResponse(), model="qwen3.5-9b")

    assert result == "Hi there"
    reasoning_chunks = [
        p
        for e, p in published
        if e == "response.stream_chunk" and p.get("is_reasoning") is True
    ]
    normal_chunks = [
        p
        for e, p in published
        if e == "response.stream_chunk" and p.get("is_reasoning") is False
    ]
    assert any(chunk.get("chunk") == "plan" for chunk in reasoning_chunks)
    assert any(chunk.get("chunk") == "ning" for chunk in reasoning_chunks)
    assert any(chunk.get("chunk") == "Hi" for chunk in normal_chunks)
    assert any(chunk.get("chunk") == " there" for chunk in normal_chunks)
