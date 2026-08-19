def test_registry_uses_file_tools_edit_by_line_range():
    from src.core.orchestration.registry_builder import example_registry
    from src.tools import file_tools

    reg = example_registry()
    entry = reg.get("edit_by_line_range")
    assert entry is not None, (
        "edit_by_line_range should be registered in example_registry"
    )
    # Ensure the registered function is the same object as exposed from file_tools
    assert entry["fn"] is file_tools.edit_by_line_range
