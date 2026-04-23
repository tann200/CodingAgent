from __future__ import annotations

from src.core.inference.llm_manager import ProviderManager, get_provider_manager


class FakeAdapter:
    def __init__(self):
        self.provider = {
            "name": "FakeProvider",
            "type": "fake",
            "supports_native_tools": True,
            "supports_parallel_tools": True,
            "supports_function_call": True,
        }
        self.default_model = "fake-model-1"
        self.supports_streaming = True
        self.context_window = 8192

    def get_loaded_context_length(self, model=None):
        return 8192


def test_get_provider_capabilities_from_adapter_instance():
    mgr = get_provider_manager()
    adapter = FakeAdapter()
    caps = mgr.get_provider_capabilities(adapter)
    assert isinstance(caps, dict)
    assert caps.get("supports_native_tools") is True
    assert caps.get("provider_family") in (
        "default",
        "mock",
        "local",
        "openai",
        "anthropic",
    )
    assert caps.get("model") == "fake-model-1"
    assert caps.get("provider_name") == "FakeProvider"
    assert caps.get("supports_streaming") is True
    assert caps.get("context_window") == 8192


def test_get_provider_capabilities_from_registered_provider_key():
    mgr = get_provider_manager()
    # register under a canonical key
    mgr._providers["fakeprovider"] = FakeAdapter()
    # ensure we treat lookup by key correctly
    caps = mgr.get_provider_capabilities("fakeprovider")
    assert caps.get("provider_name") in ("FakeProvider", "fake")
    assert caps.get("model") == "fake-model-1"
