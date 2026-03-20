import json
import time
import pytest

from src.ui.views.settings_panel import SettingsPanelController
import src.core.inference.llm_manager as llm


@pytest.fixture(autouse=True)
def restore_provider_manager():
    """Save and restore global _provider_manager state between tests."""
    pm = llm._provider_manager
    saved_providers = dict(pm._providers)
    saved_models_cache = dict(pm._models_cache)
    saved_initialized = pm._initialized
    yield
    pm._providers = saved_providers
    pm._models_cache = saved_models_cache
    pm._initialized = saved_initialized


class MockAdapter:
    def __init__(self, models=None):
        self._models = models or ["mockmodel:1"]

    def get_models_from_api(self):
        return {"models": [{"id": m} for m in self._models]}


def test_fetch_models_updates_providers_json(monkeypatch, tmp_path):
    # prepare temp providers.json
    providers = [
        {
            "name": "testprov",
            "type": "mock",
            "base_url": "http://localhost",
            "models": [],
        }
    ]
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps(providers))

    # monkeypatch resolve_config_path to return our tmp cfg
    monkeypatch.setattr(llm, "resolve_config_path", lambda path=None: cfg)

    # ensure provider manager has our provider adapter
    pm = llm._provider_manager
    pm._providers = {"testprov": MockAdapter(models=["mA", "mB"])}
    pm._models_cache = {}

    sp = SettingsPanelController()
    models = sp.fetch_models_from_provider_sync("testprov")
    assert isinstance(models, list)
    assert "mA" in models

    # check file updated
    raw = json.loads(cfg.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert raw[0].get("models") and "mA" in raw[0]["models"]


def test_fetch_models_background_updates(monkeypatch, tmp_path):
    providers = [
        {"name": "bgprov", "type": "mock", "base_url": "http://localhost", "models": []}
    ]
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps(providers))
    monkeypatch.setattr(llm, "resolve_config_path", lambda path=None: cfg)

    pm = llm._provider_manager
    pm._providers = {"bgprov": MockAdapter(models=["X", "Y"])}
    pm._models_cache = {}

    sp = SettingsPanelController()
    ok = sp.fetch_models_from_provider("bgprov")
    assert ok is True
    # wait up to 1s for background thread
    for _ in range(20):
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            if raw and raw[0].get("models"):
                break
        except json.JSONDecodeError:
            pass
        time.sleep(0.05)
    raw = json.loads(cfg.read_text(encoding="utf-8"))
    assert raw[0].get("models") and "X" in raw[0]["models"]


def test_providers_json_lock_exists():
    """H10: settings_panel must have a module-level lock guarding providers.json writes."""
    import src.ui.views.settings_panel as sp_mod
    import threading
    assert hasattr(sp_mod, "_providers_json_lock"), "H10: _providers_json_lock must exist"
    assert isinstance(sp_mod._providers_json_lock, type(threading.Lock()))


def test_providers_json_is_array_format():
    """#19: providers.json must be an array, not a single object.

    An array format lets multiple providers coexist and is handled consistently
    across all load_provider() call sites.
    """
    import json
    from pathlib import Path
    providers_path = Path(__file__).parent.parent.parent / "src" / "config" / "providers.json"
    raw = json.loads(providers_path.read_text(encoding="utf-8"))
    assert isinstance(raw, list), f"providers.json must be an array, got: {type(raw).__name__}"
    assert len(raw) >= 1
    # Each entry must be a dict with required keys
    for p in raw:
        assert isinstance(p, dict)
        assert "name" in p
        assert "type" in p


def test_select_provider_and_model_persists(monkeypatch):
    # Fake UserPrefs
    class FakePrefs:
        def __init__(self):
            self.selected_model_provider = None
            self.selected_model_name = None
            self.saved = False
            self.data = {}

        def save(self):
            self.saved = True

    fake = FakePrefs()
    monkeypatch.setattr("src.core.user_prefs.UserPrefs.load", lambda: fake)

    sp = SettingsPanelController()
    ok = sp.select_provider_and_model("someprov", "somemodel")
    assert ok is True
    assert (
        fake.selected_model_provider == "someprov"
        or fake.data.get("selected_model_provider") == "someprov"
    )
    assert fake.saved is True
