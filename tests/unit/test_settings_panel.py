"""Tests for SettingsPanelController logic (migrated from src.ui — LEGACY-03).

SettingsPanelController is a pure-core controller (depends only on src.core.*).
Tests are unchanged in logic; import path points to the canonical location at
src.core.settings.controller (moved from src.ui.views.settings_panel as part of
LEGACY-03 src/ui/ retirement).
The tests verify providers.json model fetching, background fetch, and provider
selection persistence.
"""

import json
import time
import threading
import pytest

from src.core.settings.controller import SettingsPanelController
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

    monkeypatch.setattr(llm, "resolve_config_path", lambda path=None: cfg)

    pm = llm._provider_manager
    pm._providers = {"testprov": MockAdapter(models=["mA", "mB"])}
    pm._models_cache = {}

    sp = SettingsPanelController()
    models = sp.fetch_models_from_provider_sync("testprov")
    assert isinstance(models, list)
    assert "mA" in models

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
    """H10: llm_manager must have a module-level lock guarding providers.json writes."""
    import src.core.inference.llm_manager as llm_mod

    assert hasattr(llm_mod, "_providers_json_lock"), (
        "H10: _providers_json_lock must exist in llm_manager"
    )
    # Verify it's a Lock-like object (has acquire/release)
    lock = llm_mod._providers_json_lock
    assert hasattr(lock, "acquire") and hasattr(lock, "release")


def test_providers_json_is_array_format():
    """providers.json must be an array (not a single object)."""
    import json
    from pathlib import Path

    providers_path = (
        Path(__file__).parent.parent.parent / "src" / "config" / "providers.json"
    )
    raw = json.loads(providers_path.read_text(encoding="utf-8"))
    assert isinstance(raw, list), (
        f"providers.json must be an array, got: {type(raw).__name__}"
    )
    assert len(raw) >= 1
    for p in raw:
        assert isinstance(p, dict)
        assert "name" in p
        assert "type" in p


def test_select_provider_and_model_persists(monkeypatch):
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
