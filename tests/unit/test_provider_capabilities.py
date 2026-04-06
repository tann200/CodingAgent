"""tests/unit/test_provider_capabilities.py — S6-B: get_provider_capabilities()

Tests for Orchestrator.get_provider_capabilities() and the helper
Orchestrator._map_provider_family().

Test IDs follow the pattern PC-<N> for easy cross-reference.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from src.core.orchestration.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Minimal stub adapter — avoids constructing the full Orchestrator graph
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Minimal adapter stub that only exposes the attributes read by
    get_provider_capabilities()."""

    def __init__(
        self,
        name: str = "",
        provider: Optional[Dict[str, Any]] = None,
        default_model: Optional[str] = None,
    ):
        self.name = name
        self.provider = provider
        self.default_model = default_model


def _make_orchestrator(adapter: Optional[_StubAdapter]) -> Orchestrator:
    """Return an Orchestrator instance with _adapter pre-set and no other
    initialisation (avoids hitting LLM, DB, or filesystem)."""
    orch = object.__new__(Orchestrator)
    orch._adapter = adapter  # type: ignore[attr-defined]
    return orch


# ===========================================================================
# _map_provider_family — unit tests (PC-1 through PC-15)
# ===========================================================================


class TestMapProviderFamily:
    """PC-1 … PC-15: Orchestrator._map_provider_family() classmethod."""

    def test_pc1_anthropic_exact(self):
        assert Orchestrator._map_provider_family("anthropic") == "anthropic"

    def test_pc2_anthropic_mixed_case(self):
        assert Orchestrator._map_provider_family("Anthropic") == "anthropic"

    def test_pc3_anthropic_composite(self):
        # e.g. "anthropic-vertex" should still resolve to "anthropic"
        assert Orchestrator._map_provider_family("anthropic-vertex") == "anthropic"

    def test_pc4_openai_exact(self):
        assert Orchestrator._map_provider_family("openai") == "openai"

    def test_pc5_openrouter(self):
        assert Orchestrator._map_provider_family("openrouter") == "openai"

    def test_pc6_github_copilot(self):
        assert Orchestrator._map_provider_family("github_copilot") == "openai"

    def test_pc7_copilot_shorthand(self):
        assert Orchestrator._map_provider_family("copilot") == "openai"

    def test_pc8_ollama(self):
        assert Orchestrator._map_provider_family("ollama") == "local"

    def test_pc9_lm_studio_underscore(self):
        assert Orchestrator._map_provider_family("lm_studio") == "local"

    def test_pc10_lm_studio_hyphen(self):
        # Hyphens are normalised to underscores before lookup
        assert Orchestrator._map_provider_family("lm-studio") == "local"

    def test_pc11_lmstudio_no_sep(self):
        assert Orchestrator._map_provider_family("lmstudio") == "local"

    def test_pc12_mock(self):
        assert Orchestrator._map_provider_family("mock") == "mock"

    def test_pc13_unknown_returns_default(self):
        assert Orchestrator._map_provider_family("some_unknown_provider") == "default"

    def test_pc14_empty_string_returns_default(self):
        assert Orchestrator._map_provider_family("") == "default"

    def test_pc15_uppercase_ollama(self):
        assert Orchestrator._map_provider_family("OLLAMA") == "local"


# ===========================================================================
# get_provider_capabilities — integration-style unit tests (PC-16 … PC-35)
# ===========================================================================


class TestGetProviderCapabilities:
    """PC-16 … PC-35: Orchestrator.get_provider_capabilities() with stub adapters."""

    # ── No adapter ──────────────────────────────────────────────────────────

    def test_pc16_no_adapter_returns_defaults(self):
        orch = _make_orchestrator(None)
        caps = orch.get_provider_capabilities()
        assert caps["supports_native_tools"] is False
        assert caps["provider_family"] == "default"
        assert caps["model"] is None
        assert caps["provider_name"] == ""

    # ── provider dict present ───────────────────────────────────────────────

    def test_pc17_anthropic_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "anthropic", "type": "anthropic"},
            default_model="claude-3-5-sonnet",
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "anthropic"
        assert caps["model"] == "claude-3-5-sonnet"
        assert caps["provider_name"] == "anthropic"

    def test_pc18_openai_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "openai", "type": "openai"},
            default_model="gpt-4o",
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "openai"
        assert caps["model"] == "gpt-4o"

    def test_pc19_openrouter_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "openrouter", "type": "openrouter"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "openai"

    def test_pc20_ollama_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "ollama", "type": "ollama"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "local"

    def test_pc21_lm_studio_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "lm_studio", "type": "lm_studio"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "local"

    def test_pc22_mock_via_provider_dict(self):
        adapter = _StubAdapter(
            provider={"name": "mock", "type": "mock"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "mock"

    def test_pc23_supports_native_tools_propagated(self):
        adapter = _StubAdapter(
            provider={
                "name": "openai",
                "type": "openai",
                "supports_native_tools": True,
            },
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["supports_native_tools"] is True

    def test_pc24_supports_native_tools_false_by_default(self):
        adapter = _StubAdapter(
            provider={"name": "anthropic", "type": "anthropic"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["supports_native_tools"] is False

    # ── No provider dict — fall back to adapter.name ────────────────────────

    def test_pc25_family_via_adapter_name_anthropic(self):
        adapter = _StubAdapter(name="anthropic", provider=None)
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "anthropic"

    def test_pc26_family_via_adapter_name_github_copilot(self):
        adapter = _StubAdapter(name="github_copilot", provider=None)
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "openai"

    def test_pc27_family_via_adapter_name_ollama(self):
        adapter = _StubAdapter(name="ollama", provider=None)
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "local"

    def test_pc28_family_via_adapter_name_unknown(self):
        adapter = _StubAdapter(name="my_custom_provider", provider=None)
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "default"

    # ── Type vs name resolution priority ────────────────────────────────────

    def test_pc29_type_takes_priority_over_name(self):
        # type = "anthropic", name = "my-custom-name" → should resolve via type
        adapter = _StubAdapter(
            provider={"name": "my-custom-name", "type": "anthropic"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "anthropic"

    def test_pc30_name_used_when_type_unresolved(self):
        # type is unknown, name is "openai" → should fall back to name
        adapter = _StubAdapter(
            provider={"name": "openai", "type": "xyzzy_unknown"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "openai"

    def test_pc31_provider_name_field_set_correctly(self):
        adapter = _StubAdapter(
            provider={"name": "anthropic", "type": "anthropic"},
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_name"] == "anthropic"

    def test_pc32_model_none_when_not_set(self):
        adapter = _StubAdapter(provider={"name": "mock", "type": "mock"})
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["model"] is None

    def test_pc33_model_populated_from_default_model(self):
        adapter = _StubAdapter(
            provider={"name": "anthropic", "type": "anthropic"},
            default_model="claude-opus-4",
        )
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["model"] == "claude-opus-4"

    def test_pc34_provider_dict_empty_dict_falls_back_to_name(self):
        # Empty dict: no name/type → fall back to adapter.name
        adapter = _StubAdapter(name="ollama", provider={})
        caps = _make_orchestrator(adapter).get_provider_capabilities()
        assert caps["provider_family"] == "local"

    def test_pc35_exception_in_adapter_returns_defaults(self):
        """If accessing adapter attributes raises, defaults are returned safely."""

        class _BrokenAdapter:
            @property
            def provider(self):
                raise RuntimeError("boom")

            @property
            def name(self):
                raise RuntimeError("boom")

        orch = _make_orchestrator(_BrokenAdapter())  # type: ignore[arg-type]
        caps = orch.get_provider_capabilities()
        assert caps["provider_family"] == "default"
        assert caps["supports_native_tools"] is False
