from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.orchestration.provider_capabilities import (
    get_provider_capabilities_impl,
)


class FakeManager:
    def __init__(self, caps):
        self._caps = caps

    def get_provider_capabilities(self, adapter_or_key=None):
        return dict(self._caps)


def test_get_provider_capabilities_uses_provider_manager(monkeypatch):
    fake_caps = {
        "supports_native_tools": True,
        "provider_family": "fake_family",
        "model": "x",
        "provider_name": "fake",
    }
    fake_mgr = FakeManager(fake_caps)

    # Patch get_provider_manager to return our fake manager
    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(llm_mgr, "get_provider_manager", lambda: fake_mgr)

    # Orchestrator-like object with no adapter (manager should still be consulted)
    orch = SimpleNamespace(_adapter=None)
    caps = get_provider_capabilities_impl(orch)
    assert caps == fake_caps


def test_get_provider_capabilities_fallback(monkeypatch):
    # Force the import path to raise so the function falls back to local synthesis
    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(
        llm_mgr,
        "get_provider_manager",
        lambda: (_ for _ in ()).throw(Exception("no mgr")),
    )

    # Create a fake adapter with provider dict
    adapter = SimpleNamespace(
        provider={"name": "OpenAI", "type": "openai", "supports_native_tools": True},
        default_model="gpt-4",
        name="openai",
    )
    orch = SimpleNamespace(_adapter=adapter)

    caps = get_provider_capabilities_impl(orch)
    assert isinstance(caps, dict)
    assert caps.get("supports_native_tools") is True
    assert caps.get("provider_family") in ("openai", "default")
    assert caps.get("model") == "gpt-4"


def test_provider_manager_magicmock_strings_are_filtered(monkeypatch):
    # Manager returns MagicMock-like placeholders; these should be filtered
    # so we don't expose test doubles as real provider/model names.
    fake_caps = {
        "supports_native_tools": True,
        "provider_family": "MagicMock",
        "model": "MagicMock name='mm'",
        "provider_name": "MagicMock name='pp'",
    }
    fake_mgr = FakeManager(fake_caps)

    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(llm_mgr, "get_provider_manager", lambda: fake_mgr)

    orch = SimpleNamespace(_adapter=None)
    caps = get_provider_capabilities_impl(orch)
    assert isinstance(caps, dict)
    assert caps.get("supports_native_tools") is True
    # MagicMock placeholders should be rejected
    assert caps.get("model") is None
    assert caps.get("provider_name") == ""
    # No valid provider name -> default family
    assert caps.get("provider_family") == "default"


def test_provider_manager_missing_and_no_adapter_returns_defaults(monkeypatch):
    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(
        llm_mgr,
        "get_provider_manager",
        lambda: (_ for _ in ()).throw(Exception("no mgr")),
    )

    orch = SimpleNamespace(_adapter=None)
    caps = get_provider_capabilities_impl(orch)
    assert isinstance(caps, dict)
    assert caps == {
        "supports_native_tools": False,
        "provider_family": "default",
        "model": None,
        "provider_name": "",
    }


def test_provider_family_derived_from_provider_name(monkeypatch):
    fake_caps = {
        "supports_native_tools": False,
        "provider_family": "MagicMock",
        "model": None,
        "provider_name": "OpenAI",
    }
    fake_mgr = FakeManager(fake_caps)

    import src.core.inference.llm_manager as llm_mgr

    monkeypatch.setattr(llm_mgr, "get_provider_manager", lambda: fake_mgr)

    orch = SimpleNamespace(_adapter=None)
    caps = get_provider_capabilities_impl(orch)
    assert caps.get("provider_name") == "OpenAI"
    assert caps.get("provider_family") == "openai"
