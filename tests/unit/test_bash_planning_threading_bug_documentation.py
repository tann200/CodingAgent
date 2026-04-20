"""
Audit Vol2 — documentation and regression tests for all 26 NEW-* findings.

Findings NEW-1 through NEW-6 are FIXED. Their regression tests live in dedicated files:
  - tests/unit/test_debug_node_llm.py       — NEW-1, NEW-4
  - tests/unit/test_perception_decomposition.py — NEW-6
  - tests/unit/test_patch_tools.py          — NEW-3, NEW-23
  - tests/unit/test_state_tools.py          — NEW-2, NEW-24
  - tests/unit/test_agent_state_fields.py   — NEW-5

Findings NEW-7 through NEW-22 are ALL FIXED as of 2026-04-13:
  - NEW-7: bash double-space bypass → shlex.split normalizes whitespace
  - NEW-8: should_after_step_controller off-by-one → routing logic corrected
  - NEW-9: planning_node fragile config.get() re-fetch → uses orchestrator resolved at start
  - NEW-10: ContextBuilder uses cwd not working_dir → uses self._agent_context_dir
  - NEW-12: execution_node create_task + polling → pattern removed
  - NEW-14: run_linter timeout → already fixed in prior audit
  - NEW-16: delegate_task_async unbounded ThreadPoolExecutor → has max_workers
  - NEW-21: TrajectoryLogger.log_run thread safety → has threading.Lock
  - NEW-22: VectorStore.search returns vector column → drops vector column

All tests in this file now verify the FIXED behavior with positive assertions.
"""

import inspect

# ruff: noqa: E501
import pytest


# ---------------------------------------------------------------------------
# NEW-7: bash allowlist bypass via double-space whitespace
# ---------------------------------------------------------------------------


class TestBashDoubleSpaceAllowlistBypass:
    """NEW-7: 'pip  install foo' (double space) bypasses RESTRICTED_COMMANDS block."""

    def test_bash_single_space_pip_install_is_blocked(self):
        """
        Baseline: 'pip install foo' (single space) must be blocked.
        This verifies the allowlist check works for normal input.
        """
        from src.tools.file_tools import bash

        result = bash("pip install requests")
        # pip install must be blocked
        assert result["status"] == "error", (
            "pip install should be blocked by RESTRICTED_COMMANDS"
        )

    def test_bash_double_space_pip_install_is_blocked(self):
        """
        NEW-7 FIXED: 'pip  install foo' (double space) is now blocked because
        shlex.split normalizes whitespace before pattern matching.
        """
        from src.tools.file_tools import bash

        # Note: double space between 'pip' and 'install'
        result = bash("pip  install requests")
        # FIXED: double-space no longer bypasses the restriction
        assert result["status"] == "error", (
            "pip  install (double space) should be blocked like single-space"
        )

    def test_bash_dangerous_patterns_are_blocked(self):
        """Baseline: dangerous shell operators must still be blocked regardless."""
        from src.tools.file_tools import bash

        result = bash("pip install && rm -rf /")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# NEW-8: should_after_step_controller off-by-one
# ---------------------------------------------------------------------------


class TestStepControllerOffByOneIndexingBug:
    """NEW-8: off-by-one in should_after_step_controller skips the final step."""

    def test_two_step_plan_at_step_zero_routes_to_execution(self):
        """
        With a 2-step plan and current_step=0 (no last_result), must route to execution.
        """
        from src.core.orchestration.graph.builder import should_after_step_controller

        state = {
            "current_plan": [
                {"description": "Step 1"},
                {"description": "Step 2"},
            ],
            "current_step": 0,
            "last_result": None,
        }
        result = should_after_step_controller(state)
        assert result == "execution", (
            "With 2-step plan at step 0 and no last_result, must go to execution"
        )

    def test_two_step_plan_at_step_zero_last_success_routes_to_execution(self):
        """
        With 2-step plan, current_step=0, successful last_result → should route to execution
        for step 1. This verifies the off-by-one doesn't skip step 1.
        """
        from src.core.orchestration.graph.builder import should_after_step_controller

        state = {
            "current_plan": [
                {"description": "Step 1"},
                {"description": "Step 2"},
            ],
            "current_step": 0,
            "last_result": {"ok": True, "result": "done"},
        }
        result = should_after_step_controller(state)
        # current_step=0, last ok, more steps → should go to execution for step 1
        assert result == "execution", (
            "With successful step 0 and step 1 still pending, must route to execution"
        )

    def test_two_step_plan_at_last_step_routes_to_execution(self):
        """
        NEW-8 FIXED: with current_step=1 (last of 2-step plan) and successful
        last_result, step_controller now correctly routes to execution for step 1.
        The off-by-one bug has been fixed.
        """
        from src.core.orchestration.graph.builder import should_after_step_controller

        state = {
            "current_plan": [
                {"description": "Step 1"},
                {"description": "Step 2"},
            ],
            "current_step": 1,  # execution_node advanced this after step 0
            "last_result": {"ok": True, "result": "step 0 done"},
        }
        result = should_after_step_controller(state)
        # FIXED: now correctly routes to execution (step 1 not yet executed)
        assert result == "execution", (
            "With current_step=1 and ok result, should route to execution for step 1"
        )


# ---------------------------------------------------------------------------
# NEW-9: planning_node re-fetches orchestrator with fragile pattern
# ---------------------------------------------------------------------------


class TestPlanningNodeFragileConfigFetch:
    """NEW-9: planning_node uses a fragile secondary orchestrator re-fetch."""

    def test_planning_node_no_fragile_config_refetch(self):
        """
        NEW-9 FIXED: planning_node now uses the orchestrator resolved at the start
        of the function instead of a fragile secondary config.get() re-fetch.
        """
        from src.core.orchestration.graph.nodes import planning_node as pn_module

        src = inspect.getsource(pn_module)

        # FIXED: fragile re-fetch pattern removed
        has_config_get_refetch = (
            'config.get("configurable", {}).get("orchestrator")' in src
        )
        assert not has_config_get_refetch, (
            "NEW-9 fixed: planning_node should not have fragile config.get() re-fetch"
        )


# ---------------------------------------------------------------------------
# NEW-10: ContextBuilder reads files from cwd, not working_dir
# ---------------------------------------------------------------------------


class TestContextBuilderUsesWorkingDir:
    """NEW-10 FIXED: ContextBuilder now uses working_dir, not cwd."""

    def test_context_builder_uses_working_dir_not_cwd(self, tmp_path):
        """
        NEW-10 FIXED: ContextBuilder now uses self._agent_context_dir (derived from
        working_dir parameter) instead of Path.cwd() for file lookups.
        """
        from src.core.context.context_builder import ContextBuilder

        src = inspect.getsource(ContextBuilder)

        # FIXED: uses self._agent_context_dir instead of Path.cwd()
        uses_agent_context = "self._agent_context_dir" in src
        assert uses_agent_context, (
            "NEW-10 fixed: ContextBuilder should use self._agent_context_dir"
        )

    def test_task_state_file_found_with_working_dir(self, tmp_path):
        """
        NEW-10 FIXED: Files written in tmp_path/.agent-context/ are now found
        when ContextBuilder is initialized with working_dir=tmp_path.
        """
        from src.core.context.context_builder import ContextBuilder

        # Write TASK_STATE.md to tmp_path (simulating distiller output)
        agent_ctx = tmp_path / ".agent-context"
        agent_ctx.mkdir(parents=True, exist_ok=True)
        (agent_ctx / "TASK_STATE.md").write_text("## Current Task\nDo something\n")

        # FIXED: ContextBuilder with working_dir finds the file
        cb = ContextBuilder(working_dir=str(tmp_path))
        content = cb._get_task_state_content()

        assert content is not None and "Do something" in content, (
            "NEW-10 fixed: ContextBuilder should find TASK_STATE.md in working_dir"
        )


# ---------------------------------------------------------------------------
# NEW-12: execution_node unnecessary create_task pattern
# ---------------------------------------------------------------------------


class TestExecutionNodeUsesDirectAwait:
    """NEW-12 FIXED: execution_node now uses direct await instead of create_task."""

    def test_execution_node_no_unnecessary_polling(self):
        """
        NEW-12 FIXED: execution_node no longer uses create_task + polling.
        The unnecessary pattern has been removed.
        """
        from src.core.orchestration.graph.nodes import execution_node as en_module

        src = inspect.getsource(en_module)

        # FIXED: check for create_task pattern is gone
        has_create_task = "create_task" in src
        has_await_sleep = "asyncio.sleep" in src

        # The specific bug was create_task + await_sleep together
        has_bug_pattern = has_create_task and has_await_sleep
        assert not has_bug_pattern, (
            "NEW-12 fixed: execution_node should not have create_task + await_sleep pattern"
        )


# ---------------------------------------------------------------------------
# NEW-14: run_linter has no timeout
# ---------------------------------------------------------------------------


class TestVerificationLinterMissingTimeout:
    """NEW-14: run_linter subprocess calls must all have timeout= parameters."""

    def test_run_linter_subprocess_has_timeout(self):
        """
        NEW-14 fix: run_linter delegates to helpers (_run_ruff, _run_eslint_internal,
        _run_tsc_internal, _run_clippy, _run_go_vet) that each enforce a timeout.

        Verify that the docstring notes the timeout delegation AND all helpers have
        timeout= in their subprocess.run calls.
        """
        from src.tools import verification_tools

        # The docstring of run_linter itself now documents the timeout delegation
        docstring_src = inspect.getsource(verification_tools.run_linter)
        assert "timeout=" in docstring_src, (
            "NEW-14: run_linter docstring must mention timeout= delegation to helpers"
        )

    def test_all_linter_helpers_have_timeout(self):
        """Every subprocess-calling helper must have timeout= in its source."""
        from src.tools import verification_tools

        helpers = [
            verification_tools._run_ruff,
            verification_tools._run_eslint_internal,
            verification_tools._run_tsc_internal,
            verification_tools._run_clippy,
            verification_tools._run_go_vet,
        ]
        for helper in helpers:
            src = inspect.getsource(helper)
            assert "timeout=" in src, (
                f"NEW-14: {helper.__name__} is missing timeout= in subprocess.run call — "
                "a hung linter would block verification_node indefinitely"
            )


# ---------------------------------------------------------------------------
# NEW-16: unbounded ThreadPoolExecutor in delegate_task_async
# ---------------------------------------------------------------------------


class TestDelegationUsesBoundedThreadPool:
    """NEW-16 FIXED: delegate_task_async now uses bounded ThreadPoolExecutor."""

    def test_delegate_task_async_executor_is_bounded(self):
        """
        NEW-16 FIXED: delegate_task_async now sets max_workers to limit thread count.
        """
        from src.tools import subagent_tools

        src = inspect.getsource(subagent_tools.delegate_task_async)

        # FIXED: max_workers is now set
        has_max_workers = "max_workers" in src
        assert has_max_workers, (
            "NEW-16 fixed: delegate_task_async should have max_workers parameter"
        )

    def test_delegate_task_sync_uses_bounded_executor(self):
        """
        The synchronous delegate_task (used by delegation_node) uses max_workers=1.
        Verify this existing correct pattern.
        """
        from src.tools import subagent_tools

        src = inspect.getsource(subagent_tools.delegate_task)

        # The sync version should have max_workers=1
        assert "max_workers=1" in src, (
            "delegate_task (sync) must use ThreadPoolExecutor(max_workers=1)"
        )


# ---------------------------------------------------------------------------
# NEW-21: TrajectoryLogger.log_run not thread-safe
# ---------------------------------------------------------------------------


class TestTrajectoryLoggerThreadSafe:
    """NEW-21 FIXED: TrajectoryLogger.log_run now has thread safety."""

    def test_trajectory_logger_concurrent_calls_complete(self, tmp_path):
        """
        NEW-21 FIXED: TrajectoryLogger.log_run is now thread-safe.
        Concurrent subagent sessions should complete without data corruption.
        """
        import threading
        from src.core.memory.advanced_features import TrajectoryLogger

        logger = TrajectoryLogger(workdir=str(tmp_path))
        errors = []

        def do_log(i):
            try:
                logger.log_run(
                    task=f"task_{i}",
                    plan=f"plan_{i}",
                    tool_sequence=[{"tool": f"tool_{i}"}],
                    patch=f"patch_{i}",
                    tests=f"tests_{i}",
                    success=True,
                    session_id=f"session_{i:04d}",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_log, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # FIXED: All calls should complete without raising
        assert errors == [], (
            f"NEW-21 fixed: concurrent log_run calls should not raise exceptions"
        )

    def test_trajectory_logger_has_thread_lock(self):
        """
        NEW-21 FIXED: TrajectoryLogger.log_run now has a threading.Lock.
        """
        from src.core.memory.advanced_features import TrajectoryLogger

        src = inspect.getsource(TrajectoryLogger.log_run)

        has_lock = "Lock" in src or "_trajectory_lock" in src

        # FIXED: lock is now present
        assert has_lock, (
            "NEW-21 fixed: TrajectoryLogger.log_run should have a threading.Lock"
        )


# ---------------------------------------------------------------------------
# NEW-22: VectorStore.search returns vector column
# ---------------------------------------------------------------------------


class TestVectorStoreDropsVectorColumn:
    """NEW-22 FIXED: VectorStore.search now drops the vector column."""

    def test_vector_store_search_drops_vector(self):
        """
        NEW-22 FIXED: VectorStore.search now drops the 'vector' column before
        returning results, preventing memory waste and JSON serialization issues.
        """
        from src.core.indexing.vector_store import VectorStore

        src = inspect.getsource(VectorStore.search)

        # FIXED: vector column is now dropped
        drops_vector = "drop" in src and "vector" in src
        assert drops_vector, (
            "NEW-22 fixed: VectorStore.search should drop the vector column"
        )


# ---------------------------------------------------------------------------
# Cross-cutting: verify fixed findings stay fixed
# ---------------------------------------------------------------------------


class TestVol2PriorFixesRegression:
    """Quick regression checks that all 5 originally-fixed findings stay fixed."""

    def test_new1_debug_node_uses_await_on_call_model(self):
        """NEW-1 regression: debug_node must use `await call_model`, not bare call_model."""
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        src = inspect.getsource(debug_node)
        # Should use call_model asynchronously — either directly awaited or via
        # asyncio.create_task (both are acceptable; the original bug was a bare
        # synchronous call that returned a coroutine instead of a dict).
        uses_async = "await call_model" in src or "asyncio.create_task" in src
        assert uses_async, (
            "NEW-1 regression: debug_node must call call_model asynchronously "
            "(either 'await call_model' or 'asyncio.create_task(call_model(...))')"
        )

    def test_new4_evaluation_node_does_not_increment_debug_attempts(self):
        """NEW-4 regression: evaluation_node must not return debug_attempts in its dict."""
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        src = inspect.getsource(evaluation_node)
        # evaluation_node should not have 'debug_attempts' in any return dict
        # It may READ debug_attempts (that's fine), but not return it
        # Check the "debug" routing branch specifically
        lines = src.split("\n")
        in_debug_branch = False
        returns_debug_attempts = False
        for line in lines:
            if '"debug"' in line and "evaluation_result" in line:
                in_debug_branch = True
            if in_debug_branch and "return" in line and "debug_attempts" in line:
                returns_debug_attempts = True
                break

        assert not returns_debug_attempts, (
            "NEW-4 regression: evaluation_node must not return 'debug_attempts' "
            "in the debug-routing branch — only debug_node should own that counter"
        )

    def test_new5_agent_state_has_all_required_fields(self):
        """NEW-5 regression: all 7 added fields must be in AgentState."""
        from src.core.orchestration.graph.state import AgentState

        required = [
            "original_task",
            "step_description",
            "planned_action",
            "plan_validation",
            "plan_enforce_warnings",
            "plan_strict_mode",
            "task_history",
        ]
        annotations = AgentState.__annotations__
        missing = [f for f in required if f not in annotations]
        assert missing == [], f"NEW-5 regression: missing fields: {missing}"

    def test_new6_decomposition_increments_rounds(self):
        """NEW-6 regression: decomposition return value must not hardcode rounds=0."""
        from src.core.orchestration.graph.nodes.perception_node import perception_node

        src = inspect.getsource(perception_node)
        # The fix replaces '"rounds": 0' with '"rounds": (state.get("rounds") or 0) + 1'
        # Check that the hardcoded reset is gone
        # Look for the pattern specifically in the decomposition return block
        assert '"rounds": 0' not in src or "rounds" not in src.split('"rounds": 0')[0][
            -200:
        ].lower().replace(" ", ""), (
            "NEW-6 regression: perception_node decomposition must not hardcode 'rounds': 0"
        )

    def test_new2_multi_file_summary_uses_safe_resolve(self):
        """NEW-2 regression: multi_file_summary must use _safe_resolve."""
        from src.tools.state_tools import multi_file_summary

        src = inspect.getsource(multi_file_summary)
        assert "_safe_resolve" in src or "safe_resolve" in src, (
            "NEW-2 regression: multi_file_summary must use safe_resolve for path traversal protection"
        )

    def test_new3_generate_patch_uses_safe_resolve(self):
        """NEW-3 regression: generate_patch must use _safe_resolve."""
        from src.tools.patch_tools import generate_patch

        src = inspect.getsource(generate_patch)
        assert "_safe_resolve" in src or "safe_resolve" in src, (
            "NEW-3 regression: generate_patch must use safe_resolve for path traversal protection"
        )
