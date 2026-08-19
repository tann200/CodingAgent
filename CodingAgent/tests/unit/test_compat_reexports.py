def test_file_tools_reexports_exist():
    import src.tools.file_tools as ft

    # Core file ops
    assert hasattr(ft, "read_file_chunk"), (
        "read_file_chunk should be re-exported from file_tools"
    )
    assert hasattr(ft, "write_file")
    assert hasattr(ft, "edit_by_line_range")

    # Diff preview gate API
    assert hasattr(ft, "resolve_preview_gate")
    assert hasattr(ft, "register_preview_gate")


def test_orchestrator_reexports_exist():
    import src.core.orchestration.orchestrator as orch

    # Constants and audit helper expected by older callers/tests
    assert hasattr(orch, "PERMISSION_REQUIRED_TOOLS")
    assert hasattr(orch, "DRY_RUN_BLOCKED_TOOLS")
    assert hasattr(orch, "_write_permission_audit")
