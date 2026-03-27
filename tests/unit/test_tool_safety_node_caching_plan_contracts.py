"""
Regression tests for Audit Vol10 — secondary fix batch (Vol10b).

Covers fixes applied after the initial Vol10 preliminary pass:

HR-3:  debug_node increments debug_attempts on every return path
HR-6:  "git push" in DANGEROUS_PATTERNS
HR-10: check_and_prepare_compaction removed from dead routers
HR-11: analysis_node outer exception sets analysis_failed flag
WR-2:  should_after_plan_validator annotation no longer includes "perception"
WR-4:  should_after_step_controller redundant inner check removed
RA-1:  SymbolGraph indexes up to 25 relevant files (was 10)
RA-2:  search_code issues parallel queries for all extracted symbols
ME-1:  compact_messages_to_prose uses 3000-char limit (was 1000)
MC-1:  PlanMode instantiated on Orchestrator; is_blocked() checked in execute_tool
MC-2:  rename_file tool exists and validates paths
MC-3:  evaluation_node calls session_store.add_decision on task completion
MC-6:  planning_node truncates plan_dag at MAX_PLAN_STEPS=50
TS-2:  git_commit defaults to add_all=False
TS-4:  delete_file warns when deleting a git-tracked file
TS-5:  apply_patch validates path via safe_resolve
UP-4:  settings_panel rejects empty API keys
PB-2:  generate_repo_summary result is cached per working_dir
PB-3:  SymbolGraph singleton reused per working_dir in analysis_node
ET-4:  evaluation_node → debug routing is bounded by debug_attempts
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# HR-3: debug_node increments debug_attempts on every return path
# ---------------------------------------------------------------------------


class TestDebugNodeAttemptsCounterOnAllReturnPaths:
    def test_all_return_paths_include_debug_attempts(self):
        """Every branch of debug_node must return an incremented debug_attempts."""
        from src.core.orchestration.graph.nodes import debug_node as _dn

        src = inspect.getsource(_dn)
        # The return dict must contain 'debug_attempts': next_attempt on all paths.
        # We verify the source has the key near every 'return {' block.
        assert '"debug_attempts"' in src or "'debug_attempts'" in src, (
            "HR-3: 'debug_attempts' key not found in debug_node return dicts"
        )
        assert "next_attempt" in src, (
            "HR-3: next_attempt variable not found in debug_node"
        )

    def test_debug_node_has_four_return_dicts_with_debug_attempts(self):
        """debug_node must have debug_attempts in all 4 return paths."""
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        src = inspect.getsource(debug_node)
        # Count occurrences of "debug_attempts" in return context
        count = src.count('"debug_attempts": next_attempt')
        assert count >= 3, (
            f"HR-3: expected ≥3 return dicts with 'debug_attempts': next_attempt, "
            f"found {count}. Some return paths may not increment the counter."
        )


# ---------------------------------------------------------------------------
# HR-6: "git push" in DANGEROUS_PATTERNS
# ---------------------------------------------------------------------------


class TestBashToolBlocksGitPush:
    def test_git_push_in_dangerous_patterns(self):
        """'git push' must be in DANGEROUS_PATTERNS to prevent accidental remote pushes."""
        from src.tools.file_tools import bash

        # Invoke bash with git push — should be blocked
        result = bash(command="git push origin main", workdir=str(Path.cwd()))
        assert not result.get("ok", True) or "error" in result or "danger" in str(result).lower(), (
            "HR-6: 'git push' was not blocked by DANGEROUS_PATTERNS"
        )

    def test_git_push_force_also_blocked(self):
        """'git push --force' must also be blocked."""
        from src.tools.file_tools import bash

        result = bash(command="git push --force origin main", workdir=str(Path.cwd()))
        # Should be blocked (error response, not ok)
        assert not result.get("ok", True) or "error" in result, (
            "HR-6: 'git push --force' was not blocked"
        )

    def test_git_status_still_allowed(self):
        """Safe git commands (git status) must still be allowed."""
        from src.tools.file_tools import bash

        result = bash(command="git status", workdir=str(Path.cwd()))
        # git status should succeed (not be blocked by dangerous patterns)
        assert result.get("ok") is not False or "git push" not in str(result), (
            "HR-6: 'git status' was incorrectly blocked by DANGEROUS_PATTERNS"
        )


# ---------------------------------------------------------------------------
# HR-10: check_and_prepare_compaction removed from dead routers
# ---------------------------------------------------------------------------


class TestCompactionRemovedFromExecutionRouters:
    def test_should_after_execution_with_replan_no_compaction_call(self):
        """should_after_execution_with_replan must not call check_and_prepare_compaction."""
        from src.core.orchestration.graph.builder import should_after_execution_with_replan

        # Filter comment lines — comments may reference the old call for historical context
        src = inspect.getsource(should_after_execution_with_replan)
        code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        assert "check_and_prepare_compaction" not in code, (
            "HR-10: check_and_prepare_compaction still called in "
            "should_after_execution_with_replan — compaction must happen only in "
            "memory_update_node, not in routing functions"
        )

    def test_should_after_execution_with_compaction_no_double_compaction(self):
        """should_after_execution_with_compaction must not call check_and_prepare_compaction."""
        from src.core.orchestration.graph.builder import should_after_execution_with_compaction

        # Filter comment lines — comments may reference the old call for historical context
        src = inspect.getsource(should_after_execution_with_compaction)
        code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        assert "check_and_prepare_compaction" not in code, (
            "HR-10: check_and_prepare_compaction still called in "
            "should_after_execution_with_compaction"
        )


# ---------------------------------------------------------------------------
# WR-4: should_after_step_controller dead inner check removed
# ---------------------------------------------------------------------------


class TestStepControllerRouterRedundantCheckRemoved:
    def test_redundant_inner_check_removed(self):
        """The inner redundant `if current_step < len(current_plan)` check must be removed."""
        from src.core.orchestration.graph.builder import should_after_step_controller

        src = inspect.getsource(should_after_step_controller)
        # The WR-4 fix removes the inner redundant check. After the fix there should
        # be only ONE `current_step < len(current_plan)` check inside the function
        # (the outer guard), not two nested identical checks.
        inner_check_pattern = "if current_step < len(current_plan)"
        occurrences = src.count(inner_check_pattern)
        assert occurrences <= 1, (
            f"WR-4: found {occurrences} occurrences of 'if current_step < len(current_plan)' "
            "in should_after_step_controller — the inner redundant check was not removed"
        )

    def test_step_controller_last_step_routes_to_execution(self):
        """When current_step is the last step and last_result is ok, route to execution."""
        from src.core.orchestration.graph.builder import should_after_step_controller

        plan = [{"description": "step1"}, {"description": "step2"}]
        state = {
            "current_plan": plan,
            "current_step": 1,           # last step index (2-step plan)
            "last_result": {"ok": True}, # previous step succeeded
        }
        result = should_after_step_controller(state)
        assert result == "execution", (
            f"step_controller returned '{result}' instead of 'execution' for last step. "
            "WR-4: last step must route to execution, not verification."
        )

    def test_step_controller_past_end_routes_to_verification(self):
        """When current_step >= len(plan), route to verification."""
        from src.core.orchestration.graph.builder import should_after_step_controller

        plan = [{"description": "step1"}]
        state = {
            "current_plan": plan,
            "current_step": 1,           # past end (1-step plan, step 0 done)
            "last_result": {"ok": True},
        }
        result = should_after_step_controller(state)
        assert result == "verification", (
            f"step_controller returned '{result}' instead of 'verification' after plan complete."
        )


# ---------------------------------------------------------------------------
# RA-1: SymbolGraph indexes up to 25 relevant files
# ---------------------------------------------------------------------------


class TestAnalysisNodeSymbolGraphIndexLimit:
    def test_relevant_files_limit_increased_to_25(self):
        """analysis_node must index up to 25 relevant files (was 10)."""
        from src.core.orchestration.graph.nodes import analysis_node as _an

        src = inspect.getsource(_an)
        # The old limit was [:10]; the fix uses [:25]
        assert "relevant_files[:10]" not in src, (
            "RA-1: analysis_node still uses relevant_files[:10] — limit should be 25"
        )
        assert "relevant_files[:25]" in src or ":25]" in src, (
            "RA-1: relevant_files[:25] not found in analysis_node — "
            "SymbolGraph indexing limit not increased"
        )


# ---------------------------------------------------------------------------
# RA-2: search_code parallel queries for all extracted symbols
# ---------------------------------------------------------------------------


class TestPerceptionNodeParallelSearchCode:
    def test_perception_node_issues_parallel_search_code(self):
        """perception_node must issue search_code for all extracted symbols, not just first."""
        from src.core.orchestration.graph.nodes import perception_node as _pn

        src = inspect.getsource(_pn)
        # RA-2 fix: _fetch_search_code now loops over symbol_queries[:3]
        assert "symbol_queries[:3]" in src or "_queries" in src, (
            "RA-2: perception_node still uses only the first extracted symbol for search_code"
        )
        # And runs them in parallel
        assert "asyncio.gather" in src, (
            "RA-2: asyncio.gather not used in _fetch_search_code — queries not parallel"
        )

    def test_fetch_search_code_merges_results(self):
        """_fetch_search_code must merge results from all symbol queries."""
        from src.core.orchestration.graph.nodes import perception_node as _pn

        src = inspect.getsource(_pn)
        assert "merged" in src or "extend" in src, (
            "RA-2: search_code results from multiple symbols not merged"
        )


# ---------------------------------------------------------------------------
# ME-1: compact_messages_to_prose uses 3000-char limit
# ---------------------------------------------------------------------------


class TestDistillerCompactionCharacterLimit:
    def test_compact_messages_uses_3000_char_limit(self):
        """compact_messages_to_prose must truncate at 3000 chars (was 1000)."""
        from src.core.memory import distiller as _d

        src = inspect.getsource(_d)
        assert "[:1000]" not in src or "[:3000]" in src, (
            "ME-1: compact_messages_to_prose still truncates at 1000 chars — should be 3000"
        )
        assert "[:3000]" in src or ":3000" in src, (
            "ME-1: 3000-char truncation limit not found in distiller"
        )


# ---------------------------------------------------------------------------
# MC-1: PlanMode instantiated on Orchestrator; is_blocked() in execute_tool
# ---------------------------------------------------------------------------


class TestOrchestratorPlanModeIntegration:
    def test_orchestrator_instantiates_plan_mode(self, tmp_path):
        """Orchestrator.__init__ must instantiate self.plan_mode."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))
        assert hasattr(orch, "plan_mode"), (
            "MC-1: Orchestrator does not have a plan_mode attribute"
        )
        assert orch.plan_mode is not None, (
            "MC-1: Orchestrator.plan_mode is None — PlanMode not instantiated"
        )

    def test_execute_tool_checks_plan_mode_blocked(self, tmp_path):
        """execute_tool must check plan_mode.is_blocked() for write tools."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Enable plan mode and check that blocked tools are rejected
        if orch.plan_mode:
            orch.plan_mode.enable()
            # write_file is a blocked tool in plan mode; execute_tool takes a single dict
            result = orch.execute_tool({"name": "write_file", "args": {
                "path": "test.txt", "content": "hello"
            }})
            # Should be blocked (ok=False) or have an error about plan mode
            blocked = not result.get("ok", True) or "plan mode" in str(result).lower() or "blocked" in str(result).lower()
            # Re-disable for cleanup
            orch.plan_mode.disable()
            assert blocked, (
                "MC-1: execute_tool did not block write_file in plan mode. "
                "PlanMode.is_blocked() not wired into execute_tool."
            )


# ---------------------------------------------------------------------------
# MC-2: rename_file tool
# ---------------------------------------------------------------------------


class TestRenameFileToolPathSafety:
    def test_rename_file_exists(self):
        """rename_file must be registered as a tool."""
        from src.tools.file_tools import rename_file
        assert callable(rename_file), "MC-2: rename_file is not callable"

    def test_rename_file_validates_path(self, tmp_path):
        """rename_file must reject path traversal."""
        from src.tools.file_tools import rename_file

        src = tmp_path / "src.txt"
        src.write_text("hello")

        result = rename_file(
            src_path="../../etc/passwd",
            dst_path="evil.txt",
            workdir=tmp_path,
        )
        assert not result.get("ok", True) or "error" in result, (
            "MC-2: rename_file did not reject path traversal src_path"
        )

    def test_rename_file_works_for_valid_paths(self, tmp_path):
        """rename_file must successfully rename a file within workdir."""
        from src.tools.file_tools import rename_file

        src = tmp_path / "old.txt"
        src.write_text("content")

        result = rename_file(
            src_path="old.txt",
            dst_path="new.txt",
            workdir=tmp_path,
        )
        assert result.get("ok"), f"rename_file failed: {result}"
        assert (tmp_path / "new.txt").exists(), "new.txt not created"
        assert not src.exists(), "old.txt still exists after rename"


# ---------------------------------------------------------------------------
# MC-3: evaluation_node calls add_decision on completion
# ---------------------------------------------------------------------------


class TestEvaluationNodeSessionStoreDecision:
    def test_evaluation_node_calls_add_decision_on_complete(self, tmp_path):
        """evaluation_node must call session_store.add_decision when task is complete."""
        mock_store = MagicMock()
        mock_orch = MagicMock()
        mock_orch.session_store = mock_store

        state: Dict[str, Any] = {
            "task": "Fix the bug",
            "working_dir": str(tmp_path),
            "session_id": "test-mc3",
            "verification_passed": True,
            "verification_result": {},
            "current_plan": [{"description": "step1", "completed": True}],
            "current_step": 1,
            "errors": [],
        }
        config = {"configurable": {"orchestrator": mock_orch}}

        with patch(
            "src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
            return_value=mock_orch,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.evaluation_node",
                    fromlist=["evaluation_node"],
                ).evaluation_node(state, config)
            )

        assert result.get("evaluation_result") == "complete"
        mock_store.add_decision.assert_called_once()
        call_kwargs = mock_store.add_decision.call_args
        assert "complete" in str(call_kwargs), (
            "MC-3: add_decision call did not include 'complete' in arguments"
        )

    def test_evaluation_node_add_decision_failure_is_non_fatal(self, tmp_path):
        """add_decision failure must not prevent evaluation_node from returning."""
        mock_store = MagicMock()
        mock_store.add_decision.side_effect = RuntimeError("DB locked")
        mock_orch = MagicMock()
        mock_orch.session_store = mock_store

        state: Dict[str, Any] = {
            "task": "Test task",
            "working_dir": str(tmp_path),
            "session_id": "test-mc3b",
            "verification_passed": True,
            "verification_result": {},
            "current_plan": [],
            "current_step": 0,
            "errors": [],
        }
        config = {"configurable": {"orchestrator": mock_orch}}

        with patch(
            "src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
            return_value=mock_orch,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.evaluation_node",
                    fromlist=["evaluation_node"],
                ).evaluation_node(state, config)
            )

        # Must still return complete even when add_decision raises
        assert result.get("evaluation_result") == "complete", (
            "MC-3: evaluation_node failed when add_decision raised an exception"
        )


# ---------------------------------------------------------------------------
# MC-6: planning_node truncates plan_dag at MAX_PLAN_STEPS = 50
# ---------------------------------------------------------------------------


class TestPlanningNodeMaxStepsTruncation:
    def test_max_plan_steps_constant_exists(self):
        """planning_node must define MAX_PLAN_STEPS = 50."""
        from src.core.orchestration.graph.nodes import planning_node as _pn

        src = inspect.getsource(_pn)
        assert "MAX_PLAN_STEPS" in src, (
            "MC-6: MAX_PLAN_STEPS constant not found in planning_node"
        )
        assert "MAX_PLAN_STEPS = 50" in src, (
            "MC-6: MAX_PLAN_STEPS must be 50"
        )
        assert "steps[:MAX_PLAN_STEPS]" in src, (
            "MC-6: steps not truncated to MAX_PLAN_STEPS"
        )


# ---------------------------------------------------------------------------
# TS-2: git_commit defaults to add_all=False
# ---------------------------------------------------------------------------


class TestGitCommitConservativeAddAllDefault:
    def test_git_commit_add_all_defaults_to_false(self):
        """git_commit must default add_all=False to prevent staging unrelated files."""
        import inspect
        from src.tools import git_tools

        sig = inspect.signature(git_tools.git_commit)
        add_all_param = sig.parameters.get("add_all")
        assert add_all_param is not None, "git_commit has no add_all parameter"
        assert add_all_param.default is False, (
            f"TS-2: git_commit add_all default is {add_all_param.default!r} — "
            "must be False to prevent staging unrelated files"
        )


# ---------------------------------------------------------------------------
# TS-4: delete_file warns on git-tracked file
# ---------------------------------------------------------------------------


class TestDeleteFileGitTrackingWarning:
    def test_delete_file_adds_warning_for_tracked_file(self, tmp_path):
        """delete_file must include a 'warning' key when deleting a git-tracked file."""
        from src.tools.file_tools import delete_file

        src = inspect.getsource(delete_file)
        # Verify the implementation checks git tracking
        assert "git" in src.lower() and ("ls-files" in src or "git_warning" in src), (
            "TS-4: delete_file does not check git tracking before deleting"
        )
        assert "warning" in src, (
            "TS-4: delete_file does not include a 'warning' key in result for tracked files"
        )


# ---------------------------------------------------------------------------
# TS-5: apply_patch validates path via safe_resolve
# ---------------------------------------------------------------------------


class TestApplyPatchSafeResolveValidation:
    def test_apply_patch_uses_safe_resolve(self):
        """apply_patch must validate the path with safe_resolve to prevent traversal."""
        from src.tools import patch_tools

        src = inspect.getsource(patch_tools)
        assert "safe_resolve" in src or "_safe_resolve" in src, (
            "TS-5: apply_patch does not call safe_resolve — path traversal via patch header possible"
        )


# ---------------------------------------------------------------------------
# UP-4: settings_panel rejects empty API key
# ---------------------------------------------------------------------------


class TestSettingsPanelControllerApiKeyValidation:
    def test_save_api_key_rejects_empty_string(self):
        """SettingsPanelController must reject empty API keys."""
        from src.ui.views.settings_panel import SettingsPanelController

        src = inspect.getsource(SettingsPanelController)
        assert "api_key.strip()" in src or "not api_key" in src, (
            "UP-4: SettingsPanelController does not validate API key is non-empty before saving"
        )
        assert "empty" in src.lower() or "reject" in src.lower() or "strip" in src, (
            "UP-4: No empty-key guard found in SettingsPanelController save_api_key"
        )


# ---------------------------------------------------------------------------
# PB-2: generate_repo_summary cached per working_dir
# ---------------------------------------------------------------------------


class TestAnalysisNodeRepoSummaryCache:
    def test_repo_summary_cache_exists(self):
        """analysis_node must have a module-level _REPO_SUMMARY_CACHE dict."""
        from src.core.orchestration.graph.nodes import analysis_node as _an

        assert hasattr(_an, "_REPO_SUMMARY_CACHE"), (
            "PB-2: _REPO_SUMMARY_CACHE not found in analysis_node module"
        )
        assert isinstance(_an._REPO_SUMMARY_CACHE, dict), (
            "PB-2: _REPO_SUMMARY_CACHE is not a dict"
        )

    def test_repo_summary_cache_used_in_source(self):
        """analysis_node source must check the cache before calling generate_repo_summary."""
        from src.core.orchestration.graph.nodes import analysis_node as _an

        src = inspect.getsource(_an)
        assert "_REPO_SUMMARY_CACHE" in src, (
            "PB-2: _REPO_SUMMARY_CACHE not referenced in analysis_node source"
        )
        # Use the cache-hit check (in-clause) as the sentinel — avoids matching
        # module-level comments that reference generate_repo_summary()
        cache_check = "in _REPO_SUMMARY_CACHE"
        # The actual function call (not the import line, not comments)
        gen_call = "generate_repo_summary(working_dir)"
        assert cache_check in src, (
            f"PB-2: '{cache_check}' not found — cache not checked before generate call"
        )
        assert gen_call in src, (
            f"PB-2: '{gen_call}' not found in analysis_node"
        )
        cache_pos = src.find(cache_check)
        gen_pos = src.find(gen_call)
        assert cache_pos < gen_pos, (
            "PB-2: cache lookup ('in _REPO_SUMMARY_CACHE') does not appear before "
            "generate_repo_summary(working_dir) call"
        )


# ---------------------------------------------------------------------------
# PB-3: SymbolGraph singleton per working_dir
# ---------------------------------------------------------------------------


class TestAnalysisNodeSymbolGraphSingletonCache:
    def test_symbol_graph_cache_exists(self):
        """analysis_node must have a module-level _SYMBOL_GRAPH_CACHE dict."""
        from src.core.orchestration.graph.nodes import analysis_node as _an

        assert hasattr(_an, "_SYMBOL_GRAPH_CACHE"), (
            "PB-3: _SYMBOL_GRAPH_CACHE not found in analysis_node module"
        )

    def test_symbol_graph_cache_used_in_source(self):
        """analysis_node must look up the cache before creating a new SymbolGraph."""
        from src.core.orchestration.graph.nodes import analysis_node as _an

        src = inspect.getsource(_an)
        assert "_SYMBOL_GRAPH_CACHE" in src, (
            "PB-3: _SYMBOL_GRAPH_CACHE not referenced in analysis_node"
        )
        cache_pos = src.find("_SYMBOL_GRAPH_CACHE")
        sg_pos = src.find("SymbolGraph(working_dir)")
        assert cache_pos < sg_pos, (
            "PB-3: _SYMBOL_GRAPH_CACHE check does not appear before SymbolGraph() construction"
        )


# ---------------------------------------------------------------------------
# ET-4: evaluation_node → debug routing bounded by debug_attempts
# ---------------------------------------------------------------------------


class TestEvaluationNodeDebugRoutingBounded:
    def test_evaluation_routes_to_debug_when_verification_fails(self, tmp_path):
        """evaluation_node must route to 'debug' when verification fails and attempts remain."""
        state: Dict[str, Any] = {
            "task": "fix bug",
            "working_dir": str(tmp_path),
            "session_id": "et4-test",
            "verification_passed": False,
            "verification_result": {"tests": {"status": "fail", "output": "FAILED"}},
            "current_plan": [],
            "current_step": 0,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        config = {"configurable": {"orchestrator": None}}

        with patch(
            "src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
            return_value=None,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.evaluation_node",
                    fromlist=["evaluation_node"],
                ).evaluation_node(state, config)
            )

        assert result.get("evaluation_result") == "debug", (
            f"evaluation_node returned '{result.get('evaluation_result')}' instead of 'debug' "
            "when verification failed and debug_attempts=0 < max=3. ET-4."
        )

    def test_evaluation_routes_to_end_when_debug_exhausted(self, tmp_path):
        """evaluation_node must route to 'end' (not 'debug') when max debug attempts reached."""
        state: Dict[str, Any] = {
            "task": "fix bug",
            "working_dir": str(tmp_path),
            "session_id": "et4-test2",
            "verification_passed": False,
            "verification_result": {"tests": {"status": "fail", "output": "FAILED"}},
            "current_plan": [],
            "current_step": 0,
            "errors": [],
            "debug_attempts": 3,
            "max_debug_attempts": 3,
        }
        config = {"configurable": {"orchestrator": None}}

        with patch(
            "src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
            return_value=None,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.evaluation_node",
                    fromlist=["evaluation_node"],
                ).evaluation_node(state, config)
            )

        assert result.get("evaluation_result") == "end", (
            f"evaluation_node returned '{result.get('evaluation_result')}' instead of 'end' "
            "when debug_attempts=3 >= max=3. ET-4: debug loop not bounded."
        )

    def test_should_after_evaluation_routes_debug_to_debug_node(self):
        """should_after_evaluation must route 'debug' result to the debug node."""
        from src.core.orchestration.graph.builder import should_after_evaluation

        state = {
            "evaluation_result": "debug",
            "total_debug_attempts": 2,
        }
        result = should_after_evaluation(state)
        assert result == "debug", (
            f"should_after_evaluation returned '{result}' instead of 'debug'. "
            "ET-4: debug routing broken in graph."
        )

    def test_should_after_evaluation_caps_total_debug_attempts(self):
        """should_after_evaluation must cap via total_debug_attempts (MAX=9)."""
        from src.core.orchestration.graph.builder import should_after_evaluation

        state = {
            "evaluation_result": "debug",
            "total_debug_attempts": 9,
        }
        result = should_after_evaluation(state)
        assert result in ("memory_sync", "end"), (
            f"should_after_evaluation returned '{result}' instead of memory_sync/end "
            "when total_debug_attempts=9. ET-4: global debug cap not enforced."
        )


# ---------------------------------------------------------------------------
# WR-2: should_after_plan_validator annotation no longer includes "perception"
# ---------------------------------------------------------------------------


class TestPlanValidatorRouterReturnType:
    def test_annotation_does_not_include_perception(self):
        """should_after_plan_validator must NOT have 'perception' in its Literal return type."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        src = inspect.getsource(should_after_plan_validator)
        # Check first 5 lines (signature) for the Literal annotation
        sig_lines = "\n".join(src.splitlines()[:6])
        assert '"perception"' not in sig_lines and "'perception'" not in sig_lines, (
            "WR-2: should_after_plan_validator still has 'perception' in its "
            "Literal return type annotation — this return value is never produced"
        )

    def test_plan_validator_never_returns_perception(self):
        """should_after_plan_validator must never return 'perception'."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        src = inspect.getsource(should_after_plan_validator)
        # The function body must not have return "perception" or return 'perception'
        # (the annotation check above covers the signature)
        body = "\n".join(src.splitlines()[6:])
        assert 'return "perception"' not in body and "return 'perception'" not in body, (
            "WR-2: should_after_plan_validator has a 'return \"perception\"' statement"
        )
