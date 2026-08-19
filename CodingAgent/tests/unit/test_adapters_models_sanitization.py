
from src.core.inference.adapters.ollama_adapter import OllamaAdapter

# ruff: noqa: E501
from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter


def test_ollama_update_models_filters_magicmock_and_updates_in_place(monkeypatch):
    """Ensure update_models_list filters MagicMock placeholders and updates adapter.models in-place."""
    # Instantiate with an explicit base_url to avoid reading providers.json
    adapter = OllamaAdapter(base_url="http://localhost:11434/api", models=["initial"])

    # Keep a reference to the original list object
    models_ref = adapter.models
    assert isinstance(models_ref, list)

    # Monkeypatch get_models_from_api to return a mix of valid strings, dicts and a MagicMock placeholder
    def fake_get_models_from_api():
        return {
            "models": [
                "good-model",
                "MagicMock name='mm'",
                {"name": "dict-model"},
                {"id": "dict-id"},
            ]
        }

    monkeypatch.setattr(adapter, "get_models_from_api", fake_get_models_from_api)
    # Avoid persisting to disk during the test
    monkeypatch.setattr(adapter, "_save_provider", lambda: True)

    adapter.update_models_list()

    # The adapter.models list object should be the same (in-place update)
    assert adapter.models is models_ref

    # MagicMock placeholder should be filtered out
    assert all("MagicMock" not in str(m) for m in adapter.models)

    # Expected sanitized names present
    assert "good-model" in adapter.models
    assert "dict-model" in adapter.models or "dict-id" in adapter.models


def test_openai_compat_constructor_filters_magicmock_and_whitespace():
    """OpenAICompatibleAdapter constructor should sanitise default_model and models input."""
    # Pass a MagicMock-like default_model and an input models list with empty/placeholder entries
    adapter = OpenAICompatibleAdapter(
        base_url=None,
        default_model="MagicMock name='mm'",
        models=["valid-model", "  ", "MagicMock name='mm'", {"id": "ok"}],
        name="test-openai-compat",
    )

    # default_model should be rejected as MagicMock placeholder
    assert adapter.default_model is None

    # models should contain only concrete, stripped strings
    assert "valid-model" in adapter.models
    # The empty/whitespace entry and MagicMock should be filtered out
    assert all(m.strip() for m in adapter.models)
    assert all("MagicMock" not in m for m in adapter.models)
