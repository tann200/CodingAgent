"""
Regression tests for Audit Vol3 fixes (F1–F16).

Each test class is named after the fix it covers.
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import pytest

from src.core.orchestration.graph.state import AgentState



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs: Any) -> AgentState:
    defaults: AgentState = {  # type: ignore[assignment]
        "task": "test task",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "next_action": None,
        "planned_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": [],
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": [],
        "key_symbols": [],
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "last_tool_name": None,
        "original_task": None,
        "step_description": None,
        "plan_validation": None,
        "plan_enforce_warnings": False,
        "plan_strict_mode": False,
        "task_history": [],
        "last_debug_error_type": None,
    }
    defaults.update(kwargs)  # type: ignore[call-overload]
    return defaults


# ---------------------------------------------------------------------------
# F4: user_approved stripped from execute_tool args
# ---------------------------------------------------------------------------


class TestExecutionNodeStripsLLMInjectedApprovalKey:
    def test_user_approved_stripped_before_tool_call(self, tmp_path):
        """F4: user_approved injected by LLM must not reach the tool function."""
        from src.core.orchestration.orchestrator import Orchestrator

        received_args = {}

        def spy_tool(path: str, workdir=None, **kwargs):
            received_args.update(kwargs)
            return {"status": "ok", "content": "data"}

        orch = Orchestrator(working_dir=str(tmp_path))
        orch.tool_registry.register("spy", spy_tool, description="spy")

        # LLM injects user_approved: true — it must be stripped
        tool_call = {
            "name": "spy",
            "arguments": {"path": "file.txt", "user_approved": True},
        }
        orch.execute_tool(tool_call)
        assert "user_approved" not in received_args, (
            "user_approved must be stripped from args before reaching the tool"
        )


# ---------------------------------------------------------------------------
# F5: bash blocks sed -i, tar -x, unzip without -l
# ---------------------------------------------------------------------------


class TestBashBlocksSedInplaceTarExtractUnzip:
    def _bash(self, command: str, workdir=None) -> dict:
        from src.tools.file_tools import bash
        from pathlib import Path

        return bash(command, workdir=workdir or Path("/tmp"))

    def test_sed_i_is_blocked(self):
        result = self._bash("sed -i 's/a/b/g' file.txt")
        assert result["status"] == "error"
        assert "in-place" in result["error"].lower() or "-i" in result["error"]

    def test_sed_without_i_is_allowed(self):
        """sed 's/a/b/g' without -i is safe (read-only text transform)."""
        # We can't run it against a real file in unit tests, but we can confirm
        # the security check does NOT block it.
        from src.tools.file_tools import bash
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "f.txt"
            p.write_text("hello")
            result = bash(f"sed 's/hello/world/g' {p}", workdir=Path(tmpdir))
            # Should not be blocked by security layer (may fail at OS level in CI)
            assert result.get("status") != "error" or "in-place" not in result.get(
                "error", ""
            )

    def test_tar_extract_is_blocked(self):
        result = self._bash("tar -xf archive.tar")
        assert result["status"] == "error"
        assert "extract" in result["error"].lower() or "tar" in result["error"].lower()

    def test_tar_combined_x_flag_is_blocked(self):
        result = self._bash("tar -xvf archive.tar.gz")
        assert result["status"] == "error"

    def test_tar_list_is_allowed(self):
        """tar -t (list archive contents) should pass the security check."""
        # It may still fail because no actual archive exists, but security layer
        # must not block it.
        result = self._bash("tar -tf /tmp/nonexistent.tar")
        # Not a security error
        if result["status"] == "error":
            assert "extract" not in result["error"].lower()
            assert "not allowed" not in result["error"].lower()

    def test_unzip_without_l_is_blocked(self):
        result = self._bash("unzip archive.zip")
        assert result["status"] == "error"
        assert "unzip" in result["error"].lower()

    def test_unzip_l_is_allowed(self):
        """unzip -l (list only) should pass the security check."""
        result = self._bash("unzip -l archive.zip")
        # Security check passes; OS may still fail because file doesn't exist
        if result["status"] == "error":
            assert "-l" not in result.get("error", "")
            assert "not allowed" not in result["error"].lower()


# ---------------------------------------------------------------------------
# GIT_SAFE_SUBCOMMANDS gate — bash() and bash_readonly()
# ---------------------------------------------------------------------------


class TestGitSafeSubcommandsGate:
    """Gate 3b: only allowlisted git subcommands pass without approval."""

    def _bash(self, command: str):
        from src.tools.file_tools import bash
        from pathlib import Path

        return bash(command, workdir=Path("/tmp"))

    def _bash_readonly(self, command: str):
        from src.tools.file_tools import bash_readonly
        from pathlib import Path

        return bash_readonly(command, workdir=Path("/tmp"))

    # --- bash() -----------------------------------------------------------

    def test_bash_git_push_is_blocked(self):
        result = self._bash("git push origin main")
        assert result["status"] == "error"
        assert (
            "push" in result["error"].lower()
            or "not allowed" in result["error"].lower()
        )

    def test_bash_git_reset_hard_is_blocked(self):
        result = self._bash("git reset --hard HEAD")
        assert result["status"] == "error"
        assert (
            "reset" in result["error"].lower()
            or "not allowed" in result["error"].lower()
        )

    def test_bash_git_config_global_is_blocked(self):
        result = self._bash("git config --global user.email x@y.com")
        assert result["status"] == "error"

    def test_bash_git_status_is_allowed(self):
        """git status is in GIT_SAFE_SUBCOMMANDS and must not be blocked by the security gate."""
        result = self._bash("git status")
        # The security gate must not block it (OS-level failure is fine)
        if result.get("status") == "error":
            err = result.get("error", "")
            assert "not allowed" not in err.lower(), (
                f"Security gate blocked git status: {err}"
            )

    # --- bash_readonly() --------------------------------------------------

    def test_bash_readonly_git_push_is_blocked(self):
        result = self._bash_readonly("git push origin main")
        assert result["status"] == "error"
        assert (
            "push" in result["error"].lower()
            or "not allowed" in result["error"].lower()
        )

    def test_bash_readonly_git_reset_hard_is_blocked(self):
        result = self._bash_readonly("git reset --hard HEAD")
        assert result["status"] == "error"

    def test_bash_readonly_git_config_global_is_blocked(self):
        """git config --global is NOT in GIT_SAFE_SUBCOMMANDS — must be blocked."""
        result = self._bash_readonly("git config --global user.email x@y.com")
        assert result["status"] == "error"

    def test_bash_readonly_git_status_is_allowed(self):
        result = self._bash_readonly("git status")
        if result.get("status") == "error":
            err = result.get("error", "")
            assert "not allowed" not in err.lower(), (
                f"Security gate blocked git status: {err}"
            )


# ---------------------------------------------------------------------------
# F6: edit_by_line_range implementation
# ---------------------------------------------------------------------------


class TestEditByLineRangeLineEditsAndPathSafety:
    def test_basic_replace(self, tmp_path):
        from src.tools.file_tools import edit_by_line_range
        from src.tools.guardrails import mark_file_read

        p = tmp_path / "f.txt"
        p.write_text("line1\nline2\nline3\nline4\n")
        mark_file_read(str(p.resolve()))

        result = edit_by_line_range("f.txt", 2, 3, "replaced\n", workdir=tmp_path)
        assert result["status"] == "ok"
        content = p.read_text()
        assert "replaced" in content
        assert "line1" in content
        assert "line4" in content
        assert "line2" not in content

    def test_out_of_range_returns_error(self, tmp_path):
        from src.tools.file_tools import edit_by_line_range
        from src.tools.guardrails import mark_file_read

        p = tmp_path / "f.txt"
        p.write_text("only one line\n")
        mark_file_read(str(p.resolve()))

        result = edit_by_line_range("f.txt", 5, 10, "new", workdir=tmp_path)
        assert result["status"] == "error"
        assert "Invalid line range" in result["error"]

    def test_path_traversal_rejected(self, tmp_path):
        from src.tools.file_tools import edit_by_line_range

        result = edit_by_line_range("../../etc/passwd", 1, 1, "x", workdir=tmp_path)
        # Should return not_found or error, never ok
        assert result.get("status") in ("error", "not_found")

    def test_registered_in_orchestrator(self, tmp_path):
        """F6: edit_by_line_range must be in the tool registry."""
        from src.core.orchestration.orchestrator import example_registry

        reg = example_registry()
        tool = reg.get("edit_by_line_range")
        assert tool is not None, (
            "edit_by_line_range must be registered in example_registry"
        )

    def test_in_side_effect_tools(self):
        """F6: edit_by_line_range must be in SIDE_EFFECT_TOOLS in verification_node."""
        vn_path = Path("src/core/orchestration/graph/nodes/verification_node.py")
        content = vn_path.read_text()
        assert "edit_by_line_range" in content, (
            "edit_by_line_range must appear in SIDE_EFFECT_TOOLS in verification_node"
        )


# ---------------------------------------------------------------------------
# F10: get_context_budget returns sensible values
# ---------------------------------------------------------------------------


class TestProviderContextTokenBudgetBounds:
    def test_budget_for_32k_context(self, monkeypatch):
        from src.core.inference import provider_context as pc

        monkeypatch.setattr(pc, "_load_active_context_length", lambda: 32768)
        budget = pc.get_context_budget(fraction=0.65, min_tokens=6000, max_tokens=32000)
        assert 6000 <= budget <= 32000

    def test_budget_for_128k_context(self, monkeypatch):
        from src.core.inference import provider_context as pc

        monkeypatch.setattr(pc, "_load_active_context_length", lambda: 131072)
        budget = pc.get_context_budget(fraction=0.65, min_tokens=6000, max_tokens=32000)
        assert budget == 32000  # clamped to max

    def test_budget_for_8k_context(self, monkeypatch):
        from src.core.inference import provider_context as pc

        monkeypatch.setattr(pc, "_load_active_context_length", lambda: 8192)
        budget = pc.get_context_budget(fraction=0.65, min_tokens=6000, max_tokens=32000)
        assert 6000 <= budget <= 8192

    def test_budget_minimum_respected(self, monkeypatch):
        from src.core.inference import provider_context as pc

        monkeypatch.setattr(pc, "_load_active_context_length", lambda: 4096)
        budget = pc.get_context_budget(fraction=0.65, min_tokens=6000, max_tokens=32000)
        assert budget == 6000  # clamped to min


# ---------------------------------------------------------------------------
# F13: write_file requires_split on large content
# ---------------------------------------------------------------------------


class TestWriteFileSetsRequiresSplitForLargeFiles:
    def test_write_file_large_content_sets_requires_split(self, tmp_path):
        from src.tools.file_tools import write_file

        big_content = "\n".join(f"line {i}" for i in range(250))
        result = write_file("big.txt", big_content, workdir=tmp_path)
        assert result.get("requires_split") is True, (
            "write_file with >200 lines should set requires_split=True"
        )

    def test_write_file_small_content_no_split(self, tmp_path):
        from src.tools.file_tools import write_file

        small_content = "\n".join(f"line {i}" for i in range(50))
        result = write_file("small.txt", small_content, workdir=tmp_path)
        assert result.get("requires_split") is not True


# ---------------------------------------------------------------------------
# F15: context_builder cache eviction at maxsize
# ---------------------------------------------------------------------------


class TestFileCacheLRUEvictionBeyondMaxSize:
    def test_text_cache_evicts_at_maxsize(self, tmp_path, monkeypatch):
        """F15: _TEXT_CACHE must not grow beyond _CACHE_MAX entries."""
        import src.core.context.context_builder as cb

        # Patch max to a small number for the test
        monkeypatch.setattr(cb, "_CACHE_MAX", 5)
        cb._TEXT_CACHE.clear()

        for i in range(10):
            p = tmp_path / f"file_{i}.txt"
            p.write_text(f"content {i}")
            cb.ContextBuilder._read_text_cached(p)

        assert len(cb._TEXT_CACHE) <= 5, (
            f"_TEXT_CACHE has {len(cb._TEXT_CACHE)} entries; max is 5"
        )
        cb._TEXT_CACHE.clear()

    def test_json_cache_evicts_at_maxsize(self, tmp_path, monkeypatch):
        """F15: _JSON_CACHE must not grow beyond _CACHE_MAX entries."""
        import src.core.context.context_builder as cb

        monkeypatch.setattr(cb, "_CACHE_MAX", 4)
        cb._JSON_CACHE.clear()

        for i in range(8):
            p = tmp_path / f"data_{i}.json"
            p.write_text(json.dumps({"k": i}))
            cb.ContextBuilder._read_json_cached(p)

        assert len(cb._JSON_CACHE) <= 4, (
            f"_JSON_CACHE has {len(cb._JSON_CACHE)} entries; max is 4"
        )
        cb._JSON_CACHE.clear()


# ---------------------------------------------------------------------------
# F16: orchestrator resets _session_read_files at start of run
# ---------------------------------------------------------------------------


class TestOrchestratorSessionReadFilesResetPerTask:
    def test_reads_from_previous_task_dont_carry_over(self, tmp_path):
        """F16: _session_read_files must be empty at the start of each run_agent_once call."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Simulate a file being read in a prior task
        prior_file = str((tmp_path / "prior.py").resolve())
        orch._session_read_files.add(prior_file)
        assert prior_file in orch._session_read_files  # pre-condition

        # Run a new task (will fail quickly because no real LLM, but F16 reset happens first)
        import threading

        cancel = threading.Event()
        cancel.set()  # cancel immediately so run_agent_once returns early

        orch.run_agent_once(
            system_prompt_name=None,
            messages=[{"role": "user", "content": "new task"}],
            tools={},
            cancel_event=cancel,
        )

        # _session_read_files must be empty after reset
        assert prior_file not in orch._session_read_files, (
            "F16: _session_read_files from a previous task must be cleared at run start"
        )
