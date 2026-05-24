"""Tests for src/core/env_registry.py."""

import pytest

from src.core.env_registry import (
    ENV_REGISTRY,
    EnvVarEntry,
    all_legacy_aliases,
    all_names,
    get_entry,
)

# ---------------------------------------------------------------------------
# Basic structural checks
# ---------------------------------------------------------------------------


def test_registry_is_non_empty():
    assert len(ENV_REGISTRY) > 0


def test_all_entries_are_env_var_entry_instances():
    for entry in ENV_REGISTRY:
        assert isinstance(entry, EnvVarEntry)


def test_all_canonical_names_start_with_codingagent():
    for entry in ENV_REGISTRY:
        assert entry.name.startswith("CODINGAGENT_"), (
            f"{entry.name!r} does not start with 'CODINGAGENT_'"
        )


def test_all_legacy_aliases_start_with_coding_agent():
    for entry in ENV_REGISTRY:
        if entry.legacy_alias is not None:
            assert entry.legacy_alias.startswith("CODING_AGENT_"), (
                f"legacy_alias {entry.legacy_alias!r} for {entry.name!r} "
                "does not start with 'CODING_AGENT_'"
            )


def test_no_duplicate_canonical_names():
    names = [e.name for e in ENV_REGISTRY]
    assert len(names) == len(set(names)), "Duplicate canonical names in ENV_REGISTRY"


def test_no_duplicate_legacy_aliases():
    aliases = [e.legacy_alias for e in ENV_REGISTRY if e.legacy_alias is not None]
    assert len(aliases) == len(set(aliases)), "Duplicate legacy aliases in ENV_REGISTRY"


def test_valid_type_strings():
    valid = {"str", "int", "float", "bool"}
    for entry in ENV_REGISTRY:
        assert entry.type in valid, (
            f"{entry.name!r} has unknown type {entry.type!r}"
        )


def test_sources_is_list_of_strings():
    for entry in ENV_REGISTRY:
        assert isinstance(entry.sources, list)
        for s in entry.sources:
            assert isinstance(s, str)


def test_description_is_non_empty():
    for entry in ENV_REGISTRY:
        assert entry.description.strip(), f"{entry.name!r} has empty description"


# ---------------------------------------------------------------------------
# get_entry()
# ---------------------------------------------------------------------------


def test_get_entry_known_variable():
    e = get_entry("CODINGAGENT_SANDBOX_LEVEL")
    assert e is not None
    assert e.name == "CODINGAGENT_SANDBOX_LEVEL"
    assert e.default == "workspace"
    assert e.type == "str"


def test_get_entry_unknown_returns_none():
    assert get_entry("CODINGAGENT_DOES_NOT_EXIST") is None


def test_get_entry_all_canonical_names_resolve():
    for entry in ENV_REGISTRY:
        result = get_entry(entry.name)
        assert result is entry


# ---------------------------------------------------------------------------
# all_names()
# ---------------------------------------------------------------------------


def test_all_names_sorted():
    names = all_names()
    assert names == sorted(names)


def test_all_names_count_matches_registry():
    assert len(all_names()) == len(ENV_REGISTRY)


def test_all_names_contains_known_vars():
    names = set(all_names())
    expected = {
        "CODINGAGENT_ADMIN_TOKEN",
        "CODINGAGENT_AUTONOMOUS",
        "CODINGAGENT_CONTEXT_DIR",
        "CODINGAGENT_DEBUG",
        "CODINGAGENT_SANDBOX_LEVEL",
        "CODINGAGENT_STREAM_TOKENS",
        "CODINGAGENT_STORAGE_BACKEND",
        "CODINGAGENT_SSE_QUEUE_MAX",
        "CODINGAGENT_SSE_KEEPALIVE",
        "CODINGAGENT_SSE_DROP_POLICY",
    }
    assert expected.issubset(names)


# ---------------------------------------------------------------------------
# all_legacy_aliases()
# ---------------------------------------------------------------------------


def test_all_legacy_aliases_returns_dict():
    result = all_legacy_aliases()
    assert isinstance(result, dict)


def test_all_legacy_aliases_values_are_canonical():
    canonical = set(all_names())
    for alias, canon in all_legacy_aliases().items():
        assert canon in canonical, (
            f"alias {alias!r} maps to {canon!r} which is not a canonical name"
        )


def test_all_legacy_aliases_known_mappings():
    aliases = all_legacy_aliases()
    assert aliases.get("CODING_AGENT_SANDBOX_LEVEL") is None  # no alias for this one
    assert aliases["CODING_AGENT_ADMIN_TOKEN"] == "CODINGAGENT_ADMIN_TOKEN"
    assert aliases["CODING_AGENT_STREAM_TOKENS"] == "CODINGAGENT_STREAM_TOKENS"
    assert aliases["CODING_AGENT_SSE_QUEUE_MAX"] == "CODINGAGENT_SSE_QUEUE_MAX"
    assert aliases["CODING_AGENT_STORAGE_BACKEND"] == "CODINGAGENT_STORAGE_BACKEND"


# ---------------------------------------------------------------------------
# Spot-check individual entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected_default, expected_type",
    [
        ("CODINGAGENT_SANDBOX_LEVEL", "workspace", "str"),
        ("CODINGAGENT_AUTONOMOUS", False, "bool"),
        ("CODINGAGENT_CONTEXT_DIR", ".codingAgent", "str"),
        ("CODINGAGENT_DEBUG", False, "bool"),
        ("CODINGAGENT_DISTILL_INTERVAL", 600, "int"),
        ("CODINGAGENT_HTTP_SERVER", False, "bool"),
        ("CODINGAGENT_PREVIEW_RESULT_TTL", 30.0, "float"),
        ("CODINGAGENT_SCHEDULER_HEARTBEAT", 60, "int"),
        ("CODINGAGENT_SSE_DROP_POLICY", "drop_oldest", "str"),
        ("CODINGAGENT_SSE_KEEPALIVE", 15, "int"),
        ("CODINGAGENT_SSE_QUEUE_MAX", 100, "int"),
        ("CODINGAGENT_STREAM_TOKENS", False, "bool"),
        ("CODINGAGENT_TRUSTED", False, "bool"),
    ],
)
def test_entry_defaults_and_types(name, expected_default, expected_type):
    e = get_entry(name)
    assert e is not None, f"{name!r} not found in registry"
    assert e.default == expected_default, (
        f"{name}: expected default {expected_default!r}, got {e.default!r}"
    )
    assert e.type == expected_type, (
        f"{name}: expected type {expected_type!r}, got {e.type!r}"
    )


def test_entry_sources_non_empty_for_key_vars():
    """Key variables must declare at least one source file."""
    for name in [
        "CODINGAGENT_SANDBOX_LEVEL",
        "CODINGAGENT_STREAM_TOKENS",
        "CODINGAGENT_SSE_QUEUE_MAX",
        "CODINGAGENT_STORAGE_BACKEND",
        "CODINGAGENT_ADMIN_TOKEN",
    ]:
        e = get_entry(name)
        assert e is not None
        assert len(e.sources) > 0, f"{name!r} has no sources listed"
