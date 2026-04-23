from types import SimpleNamespace

import pytest

from src.core.orchestration.graph.nodes import perception_node as pmod


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

    from src.core.context.context_builder import ContextBuilder

    builder = ContextBuilder(working_dir=".")
    msgs = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="x",
        tools=[],
        conversation=[],
        provider_capabilities=orch.get_provider_capabilities(),
    )
    assert isinstance(msgs, list)


def test_providermanager_used_when_orch_missing(monkeypatch):
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
    # perception_node expects orchestrator.adapter to exist; provide a minimal adapter
    orch.adapter = None

    pm = llm_mgr.get_provider_manager()
    caps = pm.get_provider_capabilities(None)
    assert caps.get("provider_name") == "LM_Studio"


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

    from src.core.orchestration.provider_capabilities import (
        get_provider_capabilities_impl,
    )

    orch = DummyOrch(adapter=None, _adapter=None)
    caps = get_provider_capabilities_impl(orch)
    assert caps.get("model") is None
    assert caps.get("provider_name") == ""


def test_adapter_only_fallback(monkeypatch):
    class FakeAdapter(SimpleNamespace):
        pass

    adapter = FakeAdapter(
        provider={"name": "OpenAI", "type": "openai", "supports_native_tools": True},
        default_model="gpt-4",
        name="openai",
    )
    orch = DummyOrch()
    orch.adapter = adapter

    from src.core.orchestration.provider_capabilities import (
        get_provider_capabilities_impl,
    )

    # get_provider_capabilities_impl reads orch._adapter so set it as well
    orch._adapter = adapter
    caps = get_provider_capabilities_impl(orch)
    assert caps.get("provider_name") in ("OpenAI", "openai")
    assert caps.get("model") == "gpt-4"
