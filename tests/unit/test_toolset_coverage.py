from src.core.orchestration.registry_builder import example_registry
from src.config.toolsets import loader as ts_loader
from src.tools.tools_config import TOOL_ALIASES


def test_every_registered_tool_in_some_toolset():
    reg = example_registry()
    # canonical tool names from the registry: exclude dotted aliases,
    # short-form aliases configured in TOOL_ALIASES, and test-only tools.
    _TEST_ONLY_TOOLS = {"echo"}
    canonical = [
        n for n in reg.list()
        if "." not in n and n not in TOOL_ALIASES and n not in _TEST_ONLY_TOOLS
    ]

    toolsets = ts_loader.list_available_toolsets()
    assert toolsets, "No toolsets found"

    # Build a map of tool -> appeared_in_toolset
    coverage = {t: False for t in canonical}

    for ts_name in toolsets:
        ts = ts_loader.load_toolset(ts_name)
        if not ts:
            continue
        tools = ts.get("tools") or []
        for t in tools:
            if t in coverage:
                coverage[t] = True

    missing = [t for t, present in coverage.items() if not present]
    assert not missing, f"Canonical tools missing from toolsets: {missing}"
