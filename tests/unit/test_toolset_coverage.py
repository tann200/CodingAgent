from src.core.orchestration.registry_builder import example_registry
from src.config.toolsets import loader as ts_loader


def test_every_registered_tool_in_some_toolset():
    """Verify the 3 canonical flat toolsets form a clean nested hierarchy (TASK-33)."""
    toolsets = ts_loader.list_available_toolsets()
    assert toolsets == ["compact-flat", "frontier-flat", "standard-flat"]

    compact = set(ts_loader.get_tools_for_toolset("compact-flat"))
    standard = set(ts_loader.get_tools_for_toolset("standard-flat"))
    frontier = set(ts_loader.get_tools_for_toolset("frontier-flat"))

    assert compact <= standard <= frontier
    assert "delegate_task" not in compact
    assert "load_skill" in compact
    assert "read_tool_output" in compact


def test_every_toolset_tool_is_registered():
    """Verify that every tool declared in any toolset exists in the registry."""
    reg = example_registry()
    available = set(reg.list())

    toolsets = ts_loader.list_available_toolsets()
    assert toolsets, "No toolsets found"

    missing_by_toolset = {}
    for ts_name in toolsets:
        tools = ts_loader.get_tools_for_toolset(ts_name)
        missing = [t for t in tools if t not in available]
        if missing:
            missing_by_toolset[ts_name] = missing

    assert not missing_by_toolset, f"Toolset tools missing from registry: {missing_by_toolset}"
