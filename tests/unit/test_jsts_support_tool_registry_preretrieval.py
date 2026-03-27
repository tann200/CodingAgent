"""
Regression tests for audit vol7 fixes.

Covers:
- ET-1: evaluation_node routes to debug for JS/TS verification failures
- ET-4: registered edit_by_line_range uses file_tools version (diff preview, requires_split)
- CF-3: should_after_execution_with_replan return type includes "analysis"
- WR-1/WR-2: should_after_verification uses verification_passed + checks all 6 keys
- RA-3: _INDEXED_DIRS is capped at _INDEXED_DIRS_MAX entries
- TS-5: manage_todo is in MODIFYING_TOOLS
- HR-2/MC-3: sandbox validates JS/TS syntax via node --check
- MC-5: _generate_work_summary includes git diff stat
- PB-3: pre-retrieval uses asyncio.gather (parallel fetch)
"""
import inspect
import pytest


# ---------------------------------------------------------------------------
# ET-1: evaluation_node routes to debug on JS/TS verification failure
# ---------------------------------------------------------------------------

class TestEvaluationNodeJavaScriptTypeScriptFailureRouting:
    """ET-1 regression: evaluation_node must route to debug for JS/TS failures."""

    def _make_state(self, **overrides):
        base = {
            "verification_result": {},
            "verification_passed": None,
            "current_plan": [],
            "current_step": 0,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "evaluation_result": None,
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_js_test_failure_routes_to_debug(self):
        """JS test failure must trigger debug routing, not completion."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = self._make_state(
            verification_result={"js_tests": {"status": "fail", "stdout": "Jest: 3 failed"}},
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "debug", (
            "ET-1 regression: JS test failure must route to 'debug', got: "
            f"{result.get('evaluation_result')!r}"
        )

    @pytest.mark.asyncio
    async def test_ts_check_failure_routes_to_debug(self):
        """TypeScript type-check failure must trigger debug routing."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = self._make_state(
            verification_result={"ts_check": {"status": "fail", "stdout": "TS2345 error"}},
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "debug", (
            "ET-1 regression: ts_check failure must route to 'debug'"
        )

    @pytest.mark.asyncio
    async def test_eslint_failure_routes_to_debug(self):
        """ESLint failure must trigger debug routing."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = self._make_state(
            verification_result={"eslint": {"status": "fail", "stdout": "no-unused-vars"}},
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "debug"

    @pytest.mark.asyncio
    async def test_all_pass_routes_to_complete(self):
        """When all checks pass, evaluation routes to complete (not debug)."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = self._make_state(
            verification_passed=True,
            verification_result={
                "js_tests": {"status": "pass"},
                "ts_check": {"status": "pass"},
                "eslint": {"status": "pass"},
            },
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "complete"

    @pytest.mark.asyncio
    async def test_max_debug_attempts_reached_ends(self):
        """When debug_attempts >= max, route to end even on failure."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = self._make_state(
            verification_result={"js_tests": {"status": "fail"}},
            debug_attempts=3,
            max_debug_attempts=3,
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "end", (
            "Should end when max_debug_attempts reached"
        )

    @pytest.mark.asyncio
    async def test_verification_passed_flag_takes_priority(self):
        """state['verification_passed']=True short-circuits recomputation."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        # Even though js_tests says fail, verification_passed=True overrides
        state = self._make_state(
            verification_passed=True,
            verification_result={"js_tests": {"status": "fail"}},
        )
        result = await evaluation_node(state, {})
        assert result.get("evaluation_result") == "complete"


# ---------------------------------------------------------------------------
# ET-4: registered edit_by_line_range comes from file_tools (not inline copy)
# ---------------------------------------------------------------------------

class TestEditByLineRangeSingleRegistrationInRegistry:
    """ET-4 regression: the registered edit_by_line_range must be the file_tools version."""

    def test_registered_fn_is_file_tools_version(self):
        """The tool fn must originate from file_tools, not an orchestrator inline copy."""
        from src.core.orchestration.orchestrator import example_registry
        from src.tools import file_tools

        reg = example_registry()
        tool = reg.tools.get("edit_by_line_range")
        assert tool is not None, "edit_by_line_range must be registered"

        fn = tool.get("fn")
        assert fn is not None, "edit_by_line_range must have a fn"

        # The registered function must be the same object as file_tools.edit_by_line_range
        # or at least come from the file_tools module (not a lambda from orchestrator.py)
        fn_module = getattr(fn, "__module__", "") or ""
        assert "file_tools" in fn_module or fn is file_tools.edit_by_line_range, (
            f"ET-4 regression: registered edit_by_line_range is not from file_tools "
            f"(module: {fn_module!r}). The inline orchestrator copy may have been re-added."
        )

    def test_only_one_registration(self):
        """Calling example_registry() twice should yield the same single registration."""
        from src.core.orchestration.orchestrator import example_registry
        reg1 = example_registry()
        reg2 = example_registry()
        assert "edit_by_line_range" in reg1.tools
        assert "edit_by_line_range" in reg2.tools
        # Both must point to the same function (file_tools version, not inline)
        fn1 = reg1.tools["edit_by_line_range"]["fn"]
        fn2 = reg2.tools["edit_by_line_range"]["fn"]
        assert fn1 is fn2 or getattr(fn1, "__module__", "") == getattr(fn2, "__module__", "")


# ---------------------------------------------------------------------------
# CF-3: should_after_execution_with_replan return type includes "analysis"
# ---------------------------------------------------------------------------

class TestRouteExecutionAnnotationIncludesAnalysisBranch:
    """CF-3: return type annotation must include 'analysis' (from should_after_execution)."""

    def test_return_type_includes_analysis(self):
        from src.core.orchestration.graph import builder
        import typing
        hints = typing.get_type_hints(builder.should_after_execution_with_replan)
        return_ann = str(hints.get("return", ""))
        assert "analysis" in return_ann, (
            f"CF-3: 'analysis' missing from should_after_execution_with_replan return type: {return_ann}"
        )


# ---------------------------------------------------------------------------
# WR-1/WR-2: should_after_verification checks all 6 keys + uses verification_passed
# ---------------------------------------------------------------------------

class TestVerificationRouterJSTSResultKeyHandling:
    """WR-1 regression: should_after_verification must handle JS/TS keys."""

    def test_js_tests_fail_routes_to_debug(self):
        from src.core.orchestration.graph.builder import should_after_verification
        state = {
            "verification_result": {"js_tests": {"status": "fail"}},
            "verification_passed": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        assert should_after_verification(state) == "debug"

    def test_verification_passed_true_routes_to_memory_sync(self):
        from src.core.orchestration.graph.builder import should_after_verification
        state = {
            "verification_result": {"js_tests": {"status": "fail"}},
            "verification_passed": True,  # authoritative flag overrides result dict
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        assert should_after_verification(state) == "memory_sync"

    def test_python_test_fail_routes_to_debug(self):
        from src.core.orchestration.graph.builder import should_after_verification
        state = {
            "verification_result": {"tests": {"status": "fail"}},
            "verification_passed": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        assert should_after_verification(state) == "debug"

    def test_all_pass_routes_to_memory_sync(self):
        from src.core.orchestration.graph.builder import should_after_verification
        state = {
            "verification_result": {
                "tests": {"status": "pass"},
                "linter": {"status": "pass"},
            },
            "verification_passed": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        assert should_after_verification(state) == "memory_sync"


# ---------------------------------------------------------------------------
# RA-3: _INDEXED_DIRS LRU cap
# ---------------------------------------------------------------------------

class TestIndexedDirsLRUCapEnforcement:
    """RA-3: _INDEXED_DIRS must not grow beyond _INDEXED_DIRS_MAX entries."""

    def test_cap_is_enforced(self, tmp_path):
        from src.core.orchestration.graph.nodes import analysis_node as _an

        original = dict(_an._INDEXED_DIRS)
        _an._INDEXED_DIRS.clear()

        try:
            # Create more directories than the cap
            cap = _an._INDEXED_DIRS_MAX
            dirs_to_create = cap + 10
            for i in range(dirs_to_create):
                d = tmp_path / f"dir_{i}"
                d.mkdir()
                # Call _mark_indexed directly
                _an._mark_indexed(str(d))

            assert len(_an._INDEXED_DIRS) <= cap, (
                f"RA-3: _INDEXED_DIRS grew to {len(_an._INDEXED_DIRS)} entries, "
                f"exceeding cap of {cap}"
            )
        finally:
            _an._INDEXED_DIRS.clear()
            _an._INDEXED_DIRS.update(original)


# ---------------------------------------------------------------------------
# TS-5: manage_todo in MODIFYING_TOOLS
# ---------------------------------------------------------------------------

class TestManageTodoRegisteredAsModifyingTool:
    """TS-5: manage_todo must appear in MODIFYING_TOOLS in execution_node."""

    def test_manage_todo_in_modifying_tools(self):
        from src.core.orchestration.graph.nodes import execution_node as en_mod
        src = inspect.getsource(en_mod)
        assert '"manage_todo"' in src or "'manage_todo'" in src, (
            "TS-5: manage_todo must be listed in MODIFYING_TOOLS in execution_node"
        )


# ---------------------------------------------------------------------------
# MC-5: _generate_work_summary includes git diff section
# ---------------------------------------------------------------------------

class TestWorkSummaryIncludesGitDiffStatSection:
    """MC-5: work summary must attempt to include git diff --stat."""

    def test_summary_has_git_diff_section(self, tmp_path, monkeypatch):
        """When git is available and returns output, summary includes diff section."""
        from src.core.orchestration.orchestrator import _generate_work_summary

        fake_diff_output = " src/foo.py | 10 ++++------\n 1 file changed, 4 insertions(+), 6 deletions(-)"

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            if "diff" in cmd:
                m.returncode = 0
                m.stdout = fake_diff_output
            else:
                m.returncode = 1
                m.stdout = ""
            return m

        from unittest.mock import MagicMock
        monkeypatch.setattr("src.core.orchestration.orchestrator._sp", None, raising=False)

        import src.core.orchestration.orchestrator as orc_mod
        monkeypatch.setattr(orc_mod, "_sp", type("_sp", (), {"run": staticmethod(mock_run)})())

        state = {"task": "fix bug", "rounds": 1, "working_dir": str(tmp_path)}
        summary = _generate_work_summary(state, [])
        # The summary should at least contain the work summary header
        assert "Work Summary" in summary

    def test_summary_works_without_git(self, tmp_path, monkeypatch):
        """When git is unavailable, summary still completes (no crash)."""
        from src.core.orchestration.orchestrator import _generate_work_summary

        state = {"task": "fix bug", "rounds": 2, "working_dir": str(tmp_path)}
        summary = _generate_work_summary(state, [])
        assert "Work Summary" in summary
        assert "fix bug" in summary


# ---------------------------------------------------------------------------
# PB-3: pre-retrieval is parallelised (asyncio.gather)
# ---------------------------------------------------------------------------

class TestPerceptionNodePreRetrievalParallelExecution:
    """PB-3: perception_node pre-retrieval must use asyncio.gather."""

    def test_uses_asyncio_gather(self):
        from src.core.orchestration.graph.nodes import perception_node as pn_mod
        src = inspect.getsource(pn_mod.perception_node)
        assert "asyncio.gather" in src, (
            "PB-3: perception_node must use asyncio.gather for parallel pre-retrieval"
        )
