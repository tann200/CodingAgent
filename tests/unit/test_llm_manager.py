import asyncio
import json
import time
from src.core.inference.llm_manager import (
    ProviderManager,
    _provider_manager,
    get_structured_llm,
    CircuitBreaker,
    get_circuit_breaker,
    _CIRCUIT_BREAKERS,
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
    _provider_manager.providers_config_path = str(pfile)
    pm = _provider_manager
    # reset state so initialize reloads
    pm._initialized = False
    pm._providers = {}

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

    monkeypatch.setattr("src.core.inference.llm_manager.UserPrefs.load", staticmethod(_load))

    # call get_structured_llm
    client, resolved = asyncio.run(get_structured_llm())
    assert client is not None
    # resolved should be None because model missing
    assert resolved is None
    assert len(events) >= 1


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

    def test_call_model_fast_fails_when_open(self):
        """call_model must return an error dict immediately when CB is open."""
        import asyncio
        from unittest.mock import patch, AsyncMock
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
