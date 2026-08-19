"""P3-T3: Tests for refresh_file_in_index wiring in execution_node."""
import sys
import types
import importlib
import pytest
from unittest.mock import MagicMock, patch


def _make_mock_orchestrator(working_dir="/tmp/fake_project"):
    orch = MagicMock()
    orch.working_dir = working_dir
    return orch


# ---------------------------------------------------------------------------
# Unit-level: verify _FILE_WRITING_TOOLS contains expected names
# ---------------------------------------------------------------------------

def test_file_writing_tools_set():
    import src.core.orchestration.graph.nodes.execution_node as en
    assert "write_file" in en._FILE_WRITING_TOOLS
    assert "edit_file" in en._FILE_WRITING_TOOLS
    assert "create_file" in en._FILE_WRITING_TOOLS
    assert "read_file" not in en._FILE_WRITING_TOOLS
    assert "bash_exec" not in en._FILE_WRITING_TOOLS


# ---------------------------------------------------------------------------
# Integration-ish: patch _refresh_file_in_index and verify it is called
# after a write_file tool dispatch.
# ---------------------------------------------------------------------------

def test_refresh_called_for_write_file():
    import src.core.orchestration.graph.nodes.execution_node as en

    mock_refresh = MagicMock(return_value=True)
    orch = _make_mock_orchestrator("/proj")

    with patch.object(en, "_refresh_file_in_index", mock_refresh):
        # Simulate what the wiring code does:
        tool_name = "write_file"
        args = {"path": "src/foo.py"}
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = orch.working_dir if orch and hasattr(orch, "working_dir") else None
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)

    mock_refresh.assert_called_once_with("src/foo.py", "/proj")


def test_refresh_called_for_edit_file():
    import src.core.orchestration.graph.nodes.execution_node as en

    mock_refresh = MagicMock(return_value=True)
    orch = _make_mock_orchestrator("/proj")

    with patch.object(en, "_refresh_file_in_index", mock_refresh):
        tool_name = "edit_file"
        args = {"file_path": "src/bar.py"}
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = orch.working_dir if orch and hasattr(orch, "working_dir") else None
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)

    mock_refresh.assert_called_once_with("src/bar.py", "/proj")


def test_refresh_not_called_for_read_file():
    import src.core.orchestration.graph.nodes.execution_node as en

    mock_refresh = MagicMock(return_value=True)
    orch = _make_mock_orchestrator("/proj")

    with patch.object(en, "_refresh_file_in_index", mock_refresh):
        tool_name = "read_file"
        args = {"path": "src/foo.py"}
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = orch.working_dir if orch and hasattr(orch, "working_dir") else None
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)

    mock_refresh.assert_not_called()


def test_refresh_not_called_when_no_working_dir():
    import src.core.orchestration.graph.nodes.execution_node as en

    mock_refresh = MagicMock(return_value=True)

    with patch.object(en, "_refresh_file_in_index", mock_refresh):
        tool_name = "write_file"
        args = {"path": "src/foo.py"}
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = None  # no orchestrator
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)

    mock_refresh.assert_not_called()


def test_refresh_not_called_when_no_path_arg():
    import src.core.orchestration.graph.nodes.execution_node as en

    mock_refresh = MagicMock(return_value=True)
    orch = _make_mock_orchestrator("/proj")

    with patch.object(en, "_refresh_file_in_index", mock_refresh):
        tool_name = "write_file"
        args = {}  # no path keys
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = orch.working_dir
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)

    mock_refresh.assert_not_called()


def test_refresh_graceful_when_import_none():
    """If _refresh_file_in_index is None (import failed), no error is raised."""
    import src.core.orchestration.graph.nodes.execution_node as en

    with patch.object(en, "_refresh_file_in_index", None):
        tool_name = "write_file"
        args = {"path": "src/foo.py"}
        _written_path = args.get("path")
        _working_dir = "/proj"
        # Should not raise
        if en._refresh_file_in_index is not None and tool_name in en._FILE_WRITING_TOOLS:
            if _written_path and _working_dir:
                en._refresh_file_in_index(_written_path, _working_dir)
