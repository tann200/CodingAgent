import asyncio



from src.core.startup import provider_health_check


class FakeAdapter:
    def __init__(self, models_resp=None, validate_ok=True):
        self._models_resp = models_resp
        self._validate_ok = validate_ok

    def get_models_from_api(self):
        return self._models_resp

    def validate_connection(self):
        return self._validate_ok


class FakePM:
    def __init__(self, providers):
        # providers: dict key->adapter
        self._providers = providers
        self._initialized = True

    def list_providers(self):
        return list(self._providers.keys())

    def get_provider(self, key):
        return self._providers.get(key)


def test_provider_health_filters_magicmock(monkeypatch):
    # Adapter returns models including a MagicMock placeholder
    ad = FakeAdapter(models_resp={"models": ["good-model", "MagicMock name='mm'"]})
    pm = FakePM({"lm_studio": ad})

    monkeypatch.setattr("src.core.startup.get_provider_manager", lambda: pm)

    res = asyncio.run(provider_health_check(timeout=0.1))
    assert "lm_studio" in res
    assert res["lm_studio"]["ok"] is True
    assert "MagicMock" not in ",".join(res["lm_studio"]["models"])


def test_provider_health_validate_connection(monkeypatch):
    # Adapter without get_models_from_api but validate_connection available
    class NoProbeAdapter:
        def __init__(self, ok: bool):
            self._ok = ok

        def validate_connection(self):
            return self._ok

    ad = NoProbeAdapter(False)
    pm = FakePM({"ollama": ad})

    monkeypatch.setattr("src.core.startup.get_provider_manager", lambda: pm)

    res = asyncio.run(provider_health_check(timeout=0.1))
    assert "ollama" in res
    assert res["ollama"]["ok"] is False
    assert res["ollama"]["error"] in ("validate_connection_failed", None)
