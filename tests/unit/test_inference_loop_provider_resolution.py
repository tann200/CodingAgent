from types import SimpleNamespace

import pytest


class DummyOrch(SimpleNamespace):
    pass


def test_orchestrator_level_caps_used(monkeypatch):
    orch = DummyOrch()

    def get_caps():
        return {
            "supports_native_tools": True,
            "provider_family": "openai",
            "model": "gpt-4",
            "provider_name": "OpenAI",
        }

    orch.get_provider_capabilities = get_caps

    # call the helper directly
    from src.core.orchestration.inference_loop import _resolve_provider_and_model

    provider, model = _resolve_provider_and_model(orch)
    assert provider in ("OpenAI", "openai")
    assert model == "gpt-4"


def test_providermanager_used_when_orch_missing(monkeypatch):
    # fake provider manager
    class FakeMgr:
        def get_provider_capabilities(self, adapter=None):
            return {
                "supports_native_tools": True,
                "provider_family": "local",
                "model": "gemma-4",
                "provider_name": "LM_Studio",
            }

    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(llm_mgr, "get_provider_manager", lambda: FakeMgr())

    orch = DummyOrch()
    orch._adapter = None

    from src.core.orchestration.inference_loop import _resolve_provider_and_model

    provider, model = _resolve_provider_and_model(orch)
    assert (
        provider in ("LM_Studio", "lm_studio", "lm_studio") or provider == "LM_Studio"
    )
    assert model == "gemma-4"


def test_providermanager_magicmock_filtered(monkeypatch):
    class FakeMgr:
        def get_provider_capabilities(self, adapter=None):
            return {
                "supports_native_tools": True,
                "provider_family": "MagicMock",
                "model": "MagicMock name='mm'",
                "provider_name": "MagicMock name='pp'",
            }

    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(llm_mgr, "get_provider_manager", lambda: FakeMgr())

    orch = DummyOrch(_adapter=None)
    from src.core.orchestration.inference_loop import _resolve_provider_and_model

    provider, model = _resolve_provider_and_model(orch)
    # MagicMock placeholders should be filtered
    assert model is None
    # provider may be empty string or None after sanitisation; accept both
    assert provider is None or provider == ""


def test_adapter_only_fallback(monkeypatch):
    # No orchestrator.get_provider_capabilities, no ProviderManager -> use adapter attrs
    class FakeAdapter(SimpleNamespace):
        pass

    adapter = FakeAdapter(
        provider={"name": "OpenAI", "type": "openai", "supports_native_tools": True},
        default_model="gpt-4",
        name="openai",
    )
    orch = DummyOrch(_adapter=adapter)

    from src.core.orchestration.inference_loop import _resolve_provider_and_model

    provider, model = _resolve_provider_and_model(orch)
    assert provider in ("OpenAI", "openai")
    assert model == "gpt-4"
