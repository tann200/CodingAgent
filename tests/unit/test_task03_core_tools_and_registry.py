"""Regression tests for TASK-03: core tools (list_dir, grep, generate_patch), contracts, preflight name propagation, and unified registry."""

from __future__ import annotations

from unittest.mock import patch

from src.core.orchestration.orchestrator import Orchestrator
from src.core.orchestration.registry_builder import example_registry
from src.core.orchestration.tool_registry import ToolRegistry as CoreToolRegistry
from src.tools._registry import ToolRegistry as ToolsToolRegistry
from src.tools._tool import PermissionKind


class TestTask03CoreToolsThroughPipeline:
    def test_list_files_returns_populated_items_and_filters_secrets(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "alpha.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (tmp_path / ".DS_Store").write_text("junk\n", encoding="utf-8")

        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)
        res = orch.execute_tool({"name": "list_files", "arguments": {"path": "."}})

        assert res.get("ok") is True
        payload = res.get("result") or res
        items = payload.get("items") or []
        names = [item["name"] for item in items]
        assert "subdir" in names
        assert "alpha.py" in names
        assert ".env" not in names
        assert ".DS_Store" not in names
        dir_entry = next(item for item in items if item["name"] == "subdir")
        file_entry = next(item for item in items if item["name"] == "alpha.py")
        assert dir_entry["is_dir"] is True
        assert file_entry["is_dir"] is False

    def test_grep_succeeds_on_directory_single_file_and_dash_pattern(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("value = '--flag'\nother = 42\n", encoding="utf-8")

        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)

        def _extract_matches(res: dict):
            payload = res.get("result") or res
            inner = payload.get("output") if isinstance(payload.get("output"), dict) else payload
            return inner.get("matches") or []

        # Single-file target (also forces fallback if grep -r behaves differently)
        res_file = orch.execute_tool(
            {"name": "grep", "arguments": {"pattern": "--flag", "path": "sample.py"}}
        )
        assert res_file.get("ok") is True
        assert _extract_matches(res_file), f"Expected matches on single file: {res_file}"

        # Directory target
        res_dir = orch.execute_tool(
            {"name": "grep", "arguments": {"pattern": "other", "path": "."}}
        )
        assert res_dir.get("ok") is True
        assert _extract_matches(res_dir), f"Expected matches on directory: {res_dir}"

        # Explicit Python fallback on single file
        with patch("src.tools.sandbox.run_sandboxed", side_effect=FileNotFoundError("no grep")):
            res_fb = orch.execute_tool(
                {"name": "grep", "arguments": {"pattern": "--flag", "path": "sample.py"}}
            )
        assert res_fb.get("ok") is True
        fb_matches = _extract_matches(res_fb)
        assert len(fb_matches) == 1
        assert fb_matches[0]["file_path"] == "sample.py"

    def test_generate_patch_passes_input_and_output_contracts(self, tmp_path):
        target = tmp_path / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")

        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)
        res = orch.execute_tool(
            {
                "name": "generate_patch",
                "arguments": {"path": "mod.py", "new_content": "x = 2\n"},
            }
        )

        assert res.get("ok") is True, res
        payload = res.get("result") or res
        assert "+x = 2" in payload.get("patch", "")


class TestTask03RegistryAndPreflight:
    def test_example_registry_preserves_metadata_visibility_and_permission_kind(self):
        reg = example_registry()
        entry = reg.get("read_file")
        assert entry is not None
        assert entry.get("__tool_meta__") is not None
        assert entry.get("origin") == "builtin"
        assert isinstance(entry.get("tags"), list)
        assert reg.get_permission_kind("read_file") == PermissionKind.READ_FILE
        assert reg.get_permission_kind("bash") == PermissionKind.EXECUTE_BASH

        primary_names = {s["function"]["name"] for s in reg.get_openai_functions()}
        assert "read_file" in primary_names
        assert "files_read" not in primary_names

        filtered = reg.filter_by_names(["files_read"])
        filtered_names = {s["function"]["name"] for s in filtered.get_openai_functions()}
        assert "files_read" in filtered_names

    def test_tool_registry_call_fails_closed_when_orchestrator_init_fails(self):
        core_reg = CoreToolRegistry()
        core_reg.register("echo_test", lambda x=1: {"status": "ok", "x": x})
        with patch(
            "src.core.orchestration.orchestrator.Orchestrator",
            side_effect=RuntimeError("boom"),
        ):
            try:
                core_reg.call("echo_test", x=1)
                assert False, "Expected RuntimeError when orchestrator init fails"
            except RuntimeError as exc:
                assert "Refusing unguarded tool call" in str(exc)

        tools_reg = ToolsToolRegistry()
        tools_reg.register("echo_test", lambda x=1: {"status": "ok", "x": x})
        with patch(
            "src.core.orchestration.orchestrator.Orchestrator",
            side_effect=RuntimeError("boom"),
        ):
            try:
                tools_reg.call("echo_test", x=1)
                assert False, "Expected RuntimeError when orchestrator init fails"
            except RuntimeError as exc:
                assert "Refusing unguarded tool call" in str(exc)

    def test_preflight_autocorrected_tool_name_propagates_to_execution(self, tmp_path):
        (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)
        orch._model_tier = "frontier"

        res = orch.execute_tool({"name": "list_fils", "arguments": {"path": "."}})
        assert res.get("ok") is True, res
        payload = res.get("result") or res
        assert any(item["name"] == "hello.txt" for item in payload.get("items", []))
