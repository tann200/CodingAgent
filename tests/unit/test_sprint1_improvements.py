"""Tests for improvements implemented in Sprint 1 & 2.

Covers:
- P1-4: ToolDefinition.validate_args() — argument schema validation
- P1-4: tool_preflight.py wiring of validate_args
- P2-5: repo_read_tools / repo_write_tools consolidation
- P2-7: graceful tool discovery with _OPTIONAL_MODULES
- P0-4: debug log goes to tempdir, not repo root
"""

from __future__ import annotations

import sys
import os
import importlib
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# P1-4: ToolDefinition.validate_args()
# ---------------------------------------------------------------------------


class TestToolDefinitionValidateArgs:
    """Unit tests for ToolDefinition.validate_args()."""

    def _make_defn(self, fn):
        from src.tools._tool import ToolDefinition

        return ToolDefinition(name=fn.__name__, fn=fn, description="")

    def test_no_errors_for_valid_args(self):
        def my_tool(path: str, count: int): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"path": "foo.py", "count": 3})
        assert errors == []

    def test_missing_required_field(self):
        def my_tool(path: str): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({})
        assert any("path" in e and "missing" in e for e in errors), errors

    def test_wrong_type_for_string_field(self):
        def my_tool(path: str): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"path": 123})
        assert any("path" in e and "string" in e for e in errors), errors

    def test_wrong_type_for_int_field(self):
        def my_tool(count: int): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"count": "three"})
        assert any("count" in e and "integer" in e for e in errors), errors

    def test_bool_rejected_as_integer(self):
        """Python bools are ints — validate_args should still reject them."""

        def my_tool(count: int): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"count": True})
        assert any("count" in e and "boolean" in e for e in errors), errors

    def test_optional_field_absent_is_ok(self):
        def my_tool(path: str, limit: int = 10): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"path": "foo.py"})
        assert errors == []

    def test_none_for_optional_field_is_ok(self):
        def my_tool(path: str, limit: int = 10): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"path": "foo.py", "limit": None})
        assert errors == []

    def test_does_not_raise_on_complex_signature(self):
        """validate_args must never raise, even for unusual signatures."""

        def my_tool(*args, **kwargs): ...

        defn = self._make_defn(my_tool)
        # Should return an empty list without raising
        result = defn.validate_args({"anything": 1})
        assert isinstance(result, list)

    def test_list_type_accepted(self):
        from typing import List

        def my_tool(items: List[str]): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"items": ["a", "b"]})
        assert errors == []

    def test_dict_type_accepted(self):
        from typing import Dict

        def my_tool(meta: Dict[str, str]): ...

        defn = self._make_defn(my_tool)
        errors = defn.validate_args({"meta": {"k": "v"}})
        assert errors == []


# ---------------------------------------------------------------------------
# P1-4: tool_preflight wiring
# ---------------------------------------------------------------------------


class TestPreflightValidateArgsWiring:
    """Verify that preflight_check_impl rejects calls with bad arguments."""

    def _make_orch(self, tool_name: str, fn, working_dir: str = "/tmp"):
        """Build a minimal mock orchestrator with a real ToolDefinition registered."""
        from src.tools._tool import ToolDefinition, TOOL_ATTR
        from src.tools._registry import ToolRegistry

        reg = ToolRegistry()
        defn = ToolDefinition(name=tool_name, fn=fn, description="")
        setattr(fn, TOOL_ATTR, defn)
        reg.register_definition(defn)

        orch = MagicMock()
        orch.tool_registry = reg
        orch.working_dir = working_dir
        orch._BASH_DANGEROUS_PATTERNS = []
        return orch

    def test_preflight_passes_valid_args(self):
        from src.core.orchestration.tool_preflight import preflight_check_impl

        def my_tool(path: str): ...

        orch = self._make_orch("my_tool", my_tool)
        result = preflight_check_impl(orch, {"name": "my_tool", "arguments": {"path": "foo.py"}})
        assert result.get("ok") is True, result

    def test_preflight_rejects_missing_required(self):
        from src.core.orchestration.tool_preflight import preflight_check_impl

        def my_tool(path: str): ...

        orch = self._make_orch("my_tool", my_tool)
        result = preflight_check_impl(orch, {"name": "my_tool", "arguments": {}})
        assert result.get("ok") is False
        assert result.get("error") == "argument_validation_failed"
        assert "path" in result.get("message", "")

    def test_preflight_rejects_wrong_type(self):
        from src.core.orchestration.tool_preflight import preflight_check_impl

        def my_tool(count: int): ...

        orch = self._make_orch("my_tool", my_tool)
        result = preflight_check_impl(
            orch, {"name": "my_tool", "arguments": {"count": "five"}}
        )
        assert result.get("ok") is False
        assert "count" in result.get("message", "")

    def test_preflight_unknown_tool_not_validated(self):
        from src.core.orchestration.tool_preflight import preflight_check_impl
        from src.tools._registry import ToolRegistry

        orch = MagicMock()
        orch.tool_registry = ToolRegistry()
        orch.working_dir = "/tmp"
        orch._BASH_DANGEROUS_PATTERNS = []

        result = preflight_check_impl(
            orch, {"name": "no_such_tool", "arguments": {}}
        )
        assert result.get("ok") is False
        assert result.get("error") == "tool_not_found"


# ---------------------------------------------------------------------------
# P2-5: repo_read_tools / repo_write_tools consolidation
# ---------------------------------------------------------------------------


class TestRepoToolsConsolidation:
    """Verify consolidated modules export the correct symbols."""

    def test_repo_read_tools_exports_repo_overview(self):
        import src.tools.repo_read_tools as rrt
        assert callable(getattr(rrt, "repo_overview", None))

    def test_repo_read_tools_exports_find_files(self):
        import src.tools.repo_read_tools as rrt
        assert callable(getattr(rrt, "find_files", None))

    def test_repo_read_tools_exports_analyze_repository(self):
        import src.tools.repo_read_tools as rrt
        assert callable(getattr(rrt, "analyze_repository", None))

    def test_repo_read_tools_exports_summarize_repo(self):
        import src.tools.repo_read_tools as rrt
        assert callable(getattr(rrt, "summarize_repo", None))

    def test_repo_write_tools_exports_initialize_repo_intelligence(self):
        import src.tools.repo_write_tools as rwt
        assert callable(getattr(rwt, "initialize_repo_intelligence", None))

    def test_consolidated_modules_importable(self):
        """Consolidated repo tools must remain importable."""
        import src.tools.repo_read_tools  # noqa: F401
        import src.tools.repo_write_tools  # noqa: F401

    def test_registry_has_repo_overview(self):
        """build_registry should register repo_overview via repo_read_tools."""
        from src.tools._registry import build_registry

        reg = build_registry()
        tool = reg.get("repo_overview")
        assert tool is not None, "repo_overview not found in registry"

    def test_registry_has_find_files(self):
        from src.tools._registry import build_registry

        reg = build_registry()
        assert reg.get("find_files") is not None

    def test_registry_has_analyze_repository(self):
        from src.tools._registry import build_registry

        reg = build_registry()
        assert reg.get("analyze_repository") is not None

    def test_registry_has_initialize_repo_intelligence(self):
        from src.tools._registry import build_registry

        reg = build_registry()
        assert reg.get("initialize_repo_intelligence") is not None


# ---------------------------------------------------------------------------
# P2-7: _OPTIONAL_MODULES graceful discovery
# ---------------------------------------------------------------------------


class TestOptionalModuleGracefulDiscovery:
    """Verify that ImportError for optional modules is logged at DEBUG, not WARNING."""

    def test_optional_module_import_error_is_debug(self, monkeypatch):
        import logging
        from src.tools._registry import ToolRegistry, _OPTIONAL_MODULES

        # Use a known optional module name or synthesise one
        if not _OPTIONAL_MODULES:
            pytest.skip("No optional modules defined")

        optional_name = next(iter(_OPTIONAL_MODULES))
        reg = ToolRegistry()

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("src.tools._registry")
        handler = _Handler()
        handler.setLevel(logging.DEBUG)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with patch("importlib.import_module", side_effect=ImportError("missing dep")):
                reg.discover_module_name(optional_name)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        debug_msgs = [r for r in records if r.levelno == logging.DEBUG and optional_name in r.message]
        warn_msgs = [r for r in records if r.levelno == logging.WARNING and optional_name in r.message]
        assert len(debug_msgs) >= 1, "Expected DEBUG log for optional module import failure"
        assert len(warn_msgs) == 0, f"Unexpected WARNING log: {warn_msgs}"

    def test_non_optional_module_import_error_is_warning(self):
        import logging
        from src.tools._registry import ToolRegistry

        reg = ToolRegistry()
        fake_module = "src.tools.definitely_does_not_exist_xyz"

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("src.tools._registry")
        handler = _Handler()
        handler.setLevel(logging.WARNING)
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        logger.addHandler(handler)
        try:
            reg.discover_module_name(fake_module)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        warn_msgs = [r for r in records if r.levelno == logging.WARNING and fake_module in r.message]
        assert len(warn_msgs) >= 1, "Expected WARNING for non-optional module import failure"


# ---------------------------------------------------------------------------
# P0-4: debug log path
# ---------------------------------------------------------------------------


class TestDebugLogPath:
    """Verify _dbg() in src/main.py writes to tempdir, not repo root."""

    def test_dbg_writes_to_tempdir(self, tmp_path, monkeypatch):
        # Import _dbg from src.main
        import src.main as main_mod

        # Ensure the env var is set
        monkeypatch.setenv("CODING_AGENT_DEBUG", "1")

        # Patch tempfile.gettempdir to return our tmp_path
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            # Re-import to pick up patched gettempdir isn't needed;
            # _dbg calls gettempdir() at call time.
            main_mod._dbg("test message from test")

        log_path = tmp_path / "codingagent_debug_main.log"
        assert log_path.exists(), f"Debug log not written to {log_path}"
        content = log_path.read_text()
        assert "test message from test" in content

    def test_dbg_does_not_write_to_repo_root(self, tmp_path, monkeypatch):
        """The repo root must NOT get a log file."""
        import src.main as main_mod

        monkeypatch.setenv("CODING_AGENT_DEBUG", "1")

        # We patch gettempdir to a known temp path
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            main_mod._dbg("another message")

        # The old repo-root path must not exist
        repo_root = Path(__file__).parents[2]  # tests/unit/../../ = project root
        old_log = repo_root / "tmp_debug_main.log"
        assert not old_log.exists(), f"Debug log incorrectly written to repo root: {old_log}"
