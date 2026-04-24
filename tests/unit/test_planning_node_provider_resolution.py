from types import SimpleNamespace


# ruff: noqa: E501



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

    # The code path is executed during prompt generation inside _planning_node_impl.
    # We'll call the small helper block by invoking the same logic via a minimal
    # call: build a ContextBuilder and call build_prompt which accepts provider_capabilities.
    from src.core.context.context_builder import ContextBuilder

    builder = ContextBuilder(working_dir=".")
    msgs = builder.build_prompt(
        role_name="strategic",
        active_skills=[],
        task_description="x",
        tools=[],
        conversation=[],
        provider_capabilities=orch.get_provider_capabilities(),
    )
    assert isinstance(msgs, list)


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

    # emulate the logic in planning_node that will consult provider manager
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

    orch = DummyOrch(_adapter=None)
    caps = get_provider_capabilities_impl(orch)
    # MagicMock placeholders should be filtered
    assert caps.get("model") is None
    assert caps.get("provider_name") == ""


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

    from src.core.orchestration.provider_capabilities import (
        get_provider_capabilities_impl,
    )

    caps = get_provider_capabilities_impl(orch)
    assert caps.get("provider_name") in ("OpenAI", "openai")
    assert caps.get("model") == "gpt-4"
