from __future__ import annotations

from src.core.inference.llm_manager import ProviderManager


class FakeAdapter:
    def __init__(self, name: str = "", provider: dict | None = None):
        self.name = name
        self.provider = provider or {}


def test_is_proxy_adapter_via_provider_flag():
    mgr = ProviderManager()
    a = FakeAdapter(
        name="something", provider={"type": "foo", "requires_functions": True}
    )
    assert mgr.is_proxy_adapter(a) is True


def test_add_and_remove_proxy_identifier():
    mgr = ProviderManager()
    ident = "my_test_proxy"
    # Initially not recognized
    a = FakeAdapter(name=ident)
    assert mgr.is_proxy_adapter(a) is False

    # Add identifier and check detection
    mgr.add_proxy_identifier(ident)
    assert mgr.is_proxy_adapter(a) is True

    # Remove and verify no longer detected
    mgr.remove_proxy_identifier(ident)
    assert mgr.is_proxy_adapter(a) is False
