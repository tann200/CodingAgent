"""TASK-22 regression tests: unified ToolRegistry, retired Track 1 wrappers &
formatter consolidation, canonical 3 edit primitives, and decomposed pipeline.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_single_authoritative_tool_registry_across_orchestrator_and_tools(tmp_path):
    from src.core.orchestration.orchestrator import Orchestrator
    from src.core.orchestration.registry_builder import example_registry
    from src.core.orchestration.tool_registry import ToolRegistry as CoreToolRegistry
    from src.tools._registry import ToolRegistry as ToolsToolRegistry

    assert CoreToolRegistry is ToolsToolRegistry
    reg = example_registry()
    assert isinstance(reg, ToolsToolRegistry)

    orch = Orchestrator(working_dir=str(tmp_path))
    try:
        assert isinstance(orch.tool_registry, ToolsToolRegistry)
        assert orch.tool_registry.tools is orch.tool_registry._tools
    finally:
        orch.close()


def test_canonical_edit_primitives_primary_and_legacy_aliases_hidden():
    from src.tools._registry import build_registry

    reg = build_registry()
    primary_names = {s["function"]["name"] for s in reg.get_openai_functions()}

    for canonical in ("write_file", "edit_file_atomic", "apply_patch"):
        assert canonical in primary_names

    for legacy in ("edit_file", "edit_by_line_range", "multiedit", "edit_code_block"):
        assert legacy not in primary_names
        # Legacy aliases stay registered and executable
        assert reg.get(legacy) is not None
        assert reg.get(legacy)["schema_visibility"] == "alias_only"

    # Explicit filter still exposes alias_only schemas when requested
    filtered = reg.filter_by_names(["edit_by_line_range", "multiedit"])
    filtered_names = {s["function"]["name"] for s in filtered.get_openai_functions()}
    assert filtered_names == {"edit_by_line_range", "multiedit"}


def test_retired_capability_tools_and_formatter_merged():
    repo_root = Path(__file__).resolve().parents[2]
    assert not (repo_root / "src/tools/capability_tools.py").exists()
    assert not (repo_root / "src/tools/formatter.py").exists()
    assert not (repo_root / "src/config/toolsets/frontier-full.yaml").exists()
    assert not (repo_root / "src/config/toolsets/medium-core.yaml").exists()

    from src.tools.verification_tools import format_file, run_formatter

    assert callable(format_file)
    assert callable(run_formatter)


def test_pipeline_modules_under_400_loc_and_stage_unit_tests(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_dir = repo_root / "src/core/orchestration/pipeline"
    expected_modules = {"__init__.py", "preflight.py", "permissions.py", "dispatch.py", "post_hooks.py"}
    actual = {p.name for p in pipeline_dir.glob("*.py")}
    assert expected_modules.issubset(actual)

    for py_file in pipeline_dir.glob("*.py"):
        line_count = len(py_file.read_text(encoding="utf-8").splitlines())
        assert line_count <= 400, f"{py_file.name} has {line_count} lines (limit 400)"

    # Stage 1 (preflight): read-before-write blocks unread existing file
    from src.core.orchestration.pipeline.preflight import _check_read_before_write

    target = tmp_path / "existing.py"
    target.write_text("x = 1\n", encoding="utf-8")
    fake_orch = SimpleNamespace(working_dir=tmp_path, _session_read_files=set())
    err = _check_read_before_write(fake_orch, "write_file", {"path": "existing.py"}, {"write_file"})
    assert err is not None and err["ok"] is False

    # Stage 2 (permissions): plan mode blocks write tool when unapproved
    from src.core.orchestration.pipeline.permissions import _check_plan_mode_guard

    plan_orch = SimpleNamespace(
        plan_mode=SimpleNamespace(enabled=True),
        _plan_mode_approved=False,
    )
    plan_err = _check_plan_mode_guard(plan_orch, "write_file")
    assert plan_err is not None and plan_err["ok"] is False

    # Stage 3 (dispatch): workdir confinement rejects path outside working_dir
    from src.core.orchestration.pipeline.dispatch import _dispatch_tool_call

    def _dummy_tool(workdir=None):
        return {"ok": True, "wd": str(workdir)}

    disp_orch = SimpleNamespace(working_dir=tmp_path)
    out = _dispatch_tool_call(
        disp_orch, "dummy", {"workdir": "/etc"}, {"fn": _dummy_tool}, "tc1", 5.0
    )
    assert out["ok"] is False and "outside working directory" in out["error"]

    # Stage 4 (post_hooks): truncates oversized strings & redacts secrets
    from src.core.orchestration.pipeline.post_hooks import _truncate_result_strings

    truncated = _truncate_result_strings({"output": "a" * 500}, max_chars=50)
    assert "truncated" in truncated["output"]

    # ToolExecutionService shares pipeline.permissions single source of truth
    from src.core.orchestration.tool_execution_service import ToolExecutionService

    with patch("src.core.orchestration.pipeline.permissions._check_plan_mode_guard", return_value={"ok": False, "error": "blocked"}) as spy_plan:
        verdict = ToolExecutionService._check_plan_mode("write_file", plan_orch)
        assert verdict.blocked is True
        spy_plan.assert_called_once()
