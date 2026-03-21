"""
Regression tests for Audit Vol3 fixes (F1–F16).

Each test class is named after the fix it covers.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.builder import should_after_step_controller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs: Any) -> AgentState:
    defaults: AgentState = {
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
        "tool_last_used": {},
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "files_read": {},
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
        "planned_action": None,
        "plan_validation": None,
        "plan_enforce_warnings": False,
        "plan_strict_mode": False,
        "task_history": [],
        "last_debug_error_type": None,
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# F2: should_after_step_controller — failed step routes to execution (retry)
# ---------------------------------------------------------------------------

class TestF2StepControllerFailedRouting:
    def test_failed_step_routes_to_execution(self):
        """F2: When last_result.ok=False, step_controller must retry via execution."""
        state = _make_state(
            current_plan=[{"description": "step1"}, {"description": "step2"}],
            current_step=0,
            last_result={"ok": False, "error": "something failed"},
        )
        result = should_after_step_controller(state)
        assert result == "execution", (
            f"Expected 'execution' for retry, got '{result}'"
        )

    def test_successful_step_routes_to_execution_when_more_steps(self):
        """Successful step with more steps pending → execution."""
        state = _make_state(
            current_plan=[{"description": "s1"}, {"description": "s2"}],
            current_step=1,
            last_result={"ok": True},
        )
        result = should_after_step_controller(state)
        assert result == "execution"

    def test_all_steps_done_routes_to_verification(self):
        """All plan steps completed → verification."""
        state = _make_state(
            current_plan=[{"description": "s1"}],
            current_step=1,  # past end of plan
            last_result={"ok": True},
        )
        result = should_after_step_controller(state)
        assert result == "verification"


# ---------------------------------------------------------------------------
# F4: user_approved stripped from execute_tool args
# ---------------------------------------------------------------------------

class TestF4UserApprovedStrip:
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

class TestF5BashSecurityBlocks:
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
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "f.txt"
            p.write_text("hello")
            result = bash(f"sed 's/hello/world/g' {p}", workdir=Path(tmpdir))
            # Should not be blocked by security layer (may fail at OS level in CI)
            assert result.get("status") != "error" or "in-place" not in result.get("error", "")

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
# F6: edit_by_line_range implementation
# ---------------------------------------------------------------------------

class TestF6EditByLineRange:
    def test_basic_replace(self, tmp_path):
        from src.tools.file_tools import edit_by_line_range

        p = tmp_path / "f.txt"
        p.write_text("line1\nline2\nline3\nline4\n")

        result = edit_by_line_range("f.txt", 2, 3, "replaced\n", workdir=tmp_path)
        assert result["status"] == "ok"
        content = p.read_text()
        assert "replaced" in content
        assert "line1" in content
        assert "line4" in content
        assert "line2" not in content

    def test_out_of_range_returns_error(self, tmp_path):
        from src.tools.file_tools import edit_by_line_range

        p = tmp_path / "f.txt"
        p.write_text("only one line\n")

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
        assert tool is not None, "edit_by_line_range must be registered in example_registry"

    def test_in_side_effect_tools(self):
        """F6: edit_by_line_range must be in SIDE_EFFECT_TOOLS in verification_node."""
        import re
        vn_path = Path("src/core/orchestration/graph/nodes/verification_node.py")
        content = vn_path.read_text()
        assert "edit_by_line_range" in content, (
            "edit_by_line_range must appear in SIDE_EFFECT_TOOLS in verification_node"
        )


# ---------------------------------------------------------------------------
# F7: planning_node guaranteed fallback plan
# ---------------------------------------------------------------------------

class TestF7PlanningFallback:
    def test_parse_plan_content_garbage_returns_empty(self):
        """F7 prerequisite: _parse_plan_content must return [] for unparseable output."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content
        result = _parse_plan_content("THIS IS NOT A PLAN AT ALL")
        assert result == [], f"Expected empty list, got: {result}"

    @pytest.mark.asyncio
    async def test_fallback_plan_on_llm_exception(self, tmp_path, monkeypatch):
        """F7: When LLM call throws, planning_node returns a 1-step fallback plan."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        async def _mock_call_model(*args, **kwargs):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            _mock_call_model,
        )

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = MagicMock()
        mock_orch.session_store.add_plan = MagicMock()
        mock_orch._current_task_id = "test"
        # Prevent MagicMock from auto-creating a truthy cancel_event attribute
        # (which would trigger the cancel check and cause an early return).
        mock_orch.cancel_event = None

        state = _make_state(task="do something useful", working_dir=str(tmp_path))
        config = {"configurable": {"orchestrator": mock_orch}}

        result = await planning_node(state, config)

        plan = result.get("current_plan") or []
        assert len(plan) >= 1, "planning_node must return at least a 1-step fallback plan"
        assert plan[0]["description"]


# ---------------------------------------------------------------------------
# F8: analysis_node caches index_repository call
# ---------------------------------------------------------------------------

class TestF8IndexCache:
    @pytest.mark.asyncio
    async def test_index_repository_called_once_per_dir(self, tmp_path, monkeypatch):
        """F8: index_repository must be called at most once per working directory."""
        import src.core.orchestration.graph.nodes.analysis_node as _an
        import src.core.indexing.repo_indexer as _ri

        # Clear the cache before test
        _an._INDEXED_DIRS.discard(str(tmp_path))

        call_count = []

        def _mock_index(wd):
            call_count.append(wd)

        # index_repository is imported inside the function from repo_indexer
        monkeypatch.setattr(_ri, "index_repository", _mock_index)

        # Patch VectorStore to avoid disk I/O
        try:
            import src.core.indexing.vector_store as _vs
            monkeypatch.setattr(_vs.VectorStore, "search", lambda *a, **kw: [])
        except Exception:
            pass

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}

        def _mock_get(name):
            return None

        mock_orch.tool_registry.get = _mock_get

        state = _make_state(task="find auth module", working_dir=str(tmp_path))
        config = {"configurable": {"orchestrator": mock_orch}}

        await _an.analysis_node(state, config)
        await _an.analysis_node(state, config)

        assert len(call_count) <= 1, (
            f"index_repository called {len(call_count)} times; expected at most 1 (cached after first call)"
        )
        # Clean up
        _an._INDEXED_DIRS.discard(str(tmp_path))


# ---------------------------------------------------------------------------
# F9: perception_node skips pre-retrieval on rounds > 0
# ---------------------------------------------------------------------------

class TestF9SkipPreRetrieval:
    @pytest.mark.asyncio
    async def test_pre_retrieval_skipped_on_rounds_gt_0(self, tmp_path, monkeypatch):
        """F9: search_code must NOT be called when rounds > 0."""
        from src.core.orchestration.graph.nodes import perception_node as pn_module

        calls = []

        def _search_code(**kwargs):
            calls.append(kwargs)
            return {"results": []}

        async def _mock_call_model(*args, **kwargs):
            return {
                "choices": [
                    {
                        "message": {"content": "no tool call"},
                        "finish_reason": "stop",
                    }
                ]
            }

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            _mock_call_model,
        )

        mock_orch = MagicMock()
        mock_orch.adapter = MagicMock()
        mock_orch.tool_registry.tools = {}

        def _get_tool(name):
            if name == "search_code":
                return {"fn": _search_code}
            return None

        mock_orch.tool_registry.get = _get_tool

        state = _make_state(
            task="fix the bug",
            rounds=2,  # already past round 0
            working_dir=str(tmp_path),
        )
        config = {"configurable": {"orchestrator": mock_orch}}

        await pn_module.perception_node(state, config)

        assert len(calls) == 0, (
            f"search_code should NOT be called on rounds=2, was called {len(calls)} time(s)"
        )


# ---------------------------------------------------------------------------
# F10: get_context_budget returns sensible values
# ---------------------------------------------------------------------------

class TestF10ContextBudget:
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
# F11: analysis_node symbol lookup uses regex, not first word
# ---------------------------------------------------------------------------

class TestF11SymbolLookup:
    @pytest.mark.asyncio
    async def test_implementation_task_finds_real_identifier(self, tmp_path, monkeypatch):
        """F11: 'implement authentication module' should find 'authentication', not 'implement'."""
        import src.core.orchestration.graph.nodes.analysis_node as an
        import src.core.indexing.repo_indexer as _ri

        found_names = []

        def _find_symbol(name, **kwargs):
            found_names.append(name)
            return None

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}

        def _get_tool(tool_name):
            if tool_name == "find_symbol":
                return {"fn": _find_symbol}
            return None

        mock_orch.tool_registry.get = _get_tool

        # Patch index_repository and VectorStore to avoid disk ops
        monkeypatch.setattr(_ri, "index_repository", lambda *a: None)
        try:
            import src.core.indexing.vector_store as _vs
            monkeypatch.setattr(_vs.VectorStore, "search", lambda *a, **kw: [])
        except Exception:
            pass

        # Clear index cache
        an._INDEXED_DIRS.discard(str(tmp_path))

        state = _make_state(
            task="implement authentication module",
            working_dir=str(tmp_path),
        )
        config = {"configurable": {"orchestrator": mock_orch}}
        await an.analysis_node(state, config)

        # "implement" must NOT be in the candidate list
        assert "implement" not in found_names, (
            f"'implement' should be filtered as a stopword, but got calls: {found_names}"
        )
        an._INDEXED_DIRS.discard(str(tmp_path))


# ---------------------------------------------------------------------------
# F13: write_file requires_split on large content
# ---------------------------------------------------------------------------

class TestF13RequiresSplit:
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

class TestF15CacheEviction:
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

class TestF16SessionReadFilesReset:
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
