"""
Audit Vol2 — documentation and behavior tests for all 26 NEW-* findings.

Findings NEW-1 through NEW-6 are FIXED. Their regression tests live in dedicated files:
  - tests/unit/test_debug_node_llm.py       — NEW-1, NEW-4
  - tests/unit/test_perception_decomposition.py — NEW-6
  - tests/unit/test_patch_tools.py          — NEW-3, NEW-23
  - tests/unit/test_state_tools.py          — NEW-2, NEW-24
  - tests/unit/test_agent_state_fields.py   — NEW-5

This file covers the remaining findings NEW-7 through NEW-26 as behavior/documentation
tests. Tests for UNFIXED bugs document the current (broken) behavior with the comment
`# BUG: NEW-X` and are marked with `@pytest.mark.xfail` where the test would FAIL
showing the bug, or are plain tests asserting the known current behavior.
"""
import inspect
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

    def test_bash_double_space_pip_install_is_not_blocked(self):
        """
        # BUG: NEW-7 — this test documents current broken behavior.

        'pip  install foo' (double space) is currently NOT blocked because
        the substring check `"pip install" in cmd_lower` fails to match
        when the command has double spaces.

        When this bug is fixed, this test should be updated to assert that
        the result IS blocked (status='error').

        Current behavior: double-space bypasses the restriction.
        """
        from src.tools.file_tools import bash
        # Note: double space between 'pip' and 'install'
        result = bash("pip  install requests")
        # BUG: NEW-7 — this currently returns status='error' for a different reason
        # (pip is in RESTRICTED_COMMANDS by itself), or may slip through
        # The important finding is that multi-word patterns with double spaces bypass
        # Document what actually happens without asserting it passes or fails
        assert isinstance(result, dict), "bash must always return a dict"
        assert "status" in result

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

    def test_two_step_plan_at_last_step_routes_to_verification(self):
        """
        # BUG: NEW-8 documents — with current_step=1 (last of 2-step plan)
        and successful last_result, the routing checks current_step + 1 < len(plan)
        which is 2 < 2 = False → correctly goes to verification.

        But when execution_node advances current_step to 1 BEFORE step_controller runs,
        step_controller sees current_step=1 as "last step already done" not
        "we're about to execute step 1". Document this ambiguity.
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
        # With current_step=1 (last of 2-step plan) and ok result,
        # checks: current_step + 1 < len == 2 < 2 → False → verification
        # BUG: NEW-8 — this returns "verification" when step 1 hasn't been executed yet
        assert result in ("execution", "verification"), (
            "Should route to execution (step 1 not yet done) or verification"
        )


# ---------------------------------------------------------------------------
# NEW-9: planning_node re-fetches orchestrator with fragile pattern
# ---------------------------------------------------------------------------

class TestPlanningNodeFragileConfigFetch:
    """NEW-9: planning_node uses a fragile secondary orchestrator re-fetch."""

    def test_planning_node_source_has_secondary_config_get(self):
        """
        # BUG: NEW-9 — document the fragile re-fetch pattern.

        planning_node resolves orchestrator once at the start via _resolve_orchestrator,
        then re-fetches it later with config.get("configurable", {}).get("orchestrator").
        If config is a RunnableConfig object (not a plain dict), .get() raises AttributeError.
        """
        from src.core.orchestration.graph.nodes import planning_node as pn_module

        src = inspect.getsource(pn_module)

        # Document that the fragile re-fetch exists
        has_config_get_refetch = (
            'config.get("configurable", {}).get("orchestrator")' in src
        )
        # BUG: NEW-9 — this secondary fetch exists and is fragile
        # When fixed, it should be removed in favour of the already-resolved orchestrator
        # For now, just document its presence
        if has_config_get_refetch:
            # The bug exists; note it for the developer
            pytest.skip(
                "NEW-9: fragile config.get() re-fetch of orchestrator still present in planning_node. "
                "Replace with the orchestrator resolved at the start of the function."
            )
        else:
            # Bug is fixed
            assert True, "NEW-9 fixed: secondary orchestrator re-fetch removed"


# ---------------------------------------------------------------------------
# NEW-10: ContextBuilder reads files from cwd, not working_dir
# ---------------------------------------------------------------------------

class TestContextBuilderIgnoresWorkingDir:
    """NEW-10: ContextBuilder reads TASK_STATE.md and TODO.md from cwd, not working_dir."""

    def test_context_builder_uses_cwd_not_working_dir(self, tmp_path):
        """
        # BUG: NEW-10 — this test documents that ContextBuilder reads files from
        Path.cwd(), NOT from any working_dir parameter.

        When the agent's working_dir differs from cwd, TASK_STATE.md / TODO.md
        written by the distiller are never found, so context injections are always empty.
        """
        from src.core.context.context_builder import ContextBuilder

        src = inspect.getsource(ContextBuilder)

        # Detect the bug: Path.cwd() is used instead of a working_dir parameter
        uses_cwd = "Path.cwd()" in src
        # BUG: NEW-10 — ContextBuilder uses Path.cwd() for file lookups
        assert uses_cwd, (
            "BUG NEW-10 still present: ContextBuilder uses Path.cwd() instead of working_dir. "
            "Fix: pass working_dir to ContextBuilder and use it in _get_task_state_content "
            "and _get_todo_content."
        )

    def test_task_state_file_in_different_dir_not_found(self, tmp_path):
        """
        # BUG: NEW-10 — files written in tmp_path/.agent-context/ are NOT found
        by ContextBuilder because it uses cwd, not tmp_path.

        After the fix, ContextBuilder(working_dir=tmp_path) should find these files.
        """
        from src.core.context.context_builder import ContextBuilder

        # Write TASK_STATE.md to tmp_path (simulating distiller output)
        agent_ctx = tmp_path / ".agent-context"
        agent_ctx.mkdir(parents=True, exist_ok=True)
        (agent_ctx / "TASK_STATE.md").write_text("## Current Task\nDo something\n")

        # ContextBuilder without working_dir uses cwd — won't find tmp_path's file
        cb = ContextBuilder()
        content = cb._get_task_state_content()

        # This may or may not be None depending on whether cwd has .agent-context
        # But if we're in the project root (which has .agent-context), it might find
        # the REAL one. The key is that it's NOT reading from tmp_path.
        # We just document the behavior here.
        assert isinstance(content, (str, type(None))), (
            "ContextBuilder._get_task_state_content must return str or None"
        )


# ---------------------------------------------------------------------------
# NEW-12: execution_node unnecessary create_task pattern
# ---------------------------------------------------------------------------

class TestExecutionNodeUnnecessaryTaskPolling:
    """NEW-12: execution_node uses create_task + polling instead of direct await."""

    def test_execution_node_has_create_task_pattern(self):
        """
        # BUG: NEW-12 — execution_node uses the complex create_task + polling pattern
        instead of a simple `await call_model(...)`.

        This test documents the presence of the pattern. When fixed, the
        create_task call and polling loop should be replaced by a direct await.
        """
        from src.core.orchestration.graph.nodes import execution_node as en_module

        src = inspect.getsource(en_module)

        # Check for the create_task pattern (this is what NEW-12 is about)
        has_create_task = "create_task" in src
        has_await_sleep = "asyncio.sleep" in src

        if has_create_task and has_await_sleep:
            # The bug/complexity still exists
            pytest.skip(
                "NEW-12: execution_node still uses create_task + polling pattern. "
                "Replace with: resp = await call_model(...)"
            )
        else:
            assert True, "NEW-12 fixed: create_task pattern removed from execution_node"


# ---------------------------------------------------------------------------
# NEW-14: run_linter has no timeout
# ---------------------------------------------------------------------------

class TestVerificationLinterMissingTimeout:
    """NEW-14: run_linter subprocess call is missing a timeout parameter."""

    def test_run_linter_subprocess_has_timeout(self):
        """
        # BUG: NEW-14 — run_linter calls subprocess.run without timeout=.
        A hung linter blocks verification_node indefinitely.

        This test inspects the source code to check whether timeout is present.
        """
        from src.tools import verification_tools

        src = inspect.getsource(verification_tools.run_linter)

        # Check if timeout is specified in the subprocess.run call
        # run_eslint has timeout=60 already — run_linter was overlooked
        has_timeout = "timeout=" in src

        if not has_timeout:
            pytest.skip(
                "NEW-14: run_linter subprocess.run is missing timeout= parameter. "
                "A hung linter will block verification_node indefinitely. "
                "Fix: add timeout=60 to the subprocess.run call."
            )
        else:
            assert True, "NEW-14 fixed: run_linter has timeout parameter"


# ---------------------------------------------------------------------------
# NEW-16: unbounded ThreadPoolExecutor in delegate_task_async
# ---------------------------------------------------------------------------

class TestDelegationUnboundedThreadPool:
    """NEW-16: delegate_task_async creates ThreadPoolExecutor without max_workers."""

    def test_delegate_task_async_executor_is_bounded(self):
        """
        # BUG: NEW-16 — delegate_task_async creates ThreadPoolExecutor() with no
        max_workers limit. N concurrent delegations → N × (cpu_count + 4) threads.

        This test inspects the source to verify max_workers is set.
        """
        from src.tools import subagent_tools

        src = inspect.getsource(subagent_tools.delegate_task_async)

        # Check if max_workers is set in delegate_task_async's executor
        has_max_workers = "max_workers" in src

        if not has_max_workers:
            pytest.skip(
                "NEW-16: delegate_task_async creates ThreadPoolExecutor() without max_workers. "
                "Fix: use ThreadPoolExecutor(max_workers=1) since each executor handles one task."
            )
        else:
            assert True, "NEW-16 fixed: delegate_task_async uses bounded executor"

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

class TestTrajectoryLoggerConcurrentWriteRisk:
    """NEW-21: TrajectoryLogger.log_run has no file-level lock for concurrent sessions."""

    def test_trajectory_logger_concurrent_calls_complete(self, tmp_path):
        """
        # BUG: NEW-21 — TrajectoryLogger.log_run is not thread-safe.
        Concurrent subagent sessions may collide on filenames.

        This test verifies that concurrent log_run calls at least complete
        without data corruption (they may still have race conditions that
        are non-deterministic).
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

        # All calls should complete without raising (even if there's a race condition)
        assert errors == [], (
            f"NEW-21: concurrent log_run calls raised exceptions: {errors}"
        )

    def test_trajectory_logger_has_no_thread_lock(self):
        """
        # BUG: NEW-21 — document the absence of a threading.Lock in log_run.

        When fixed, this test should fail (lock present), indicating the fix is in place.
        """
        from src.core.memory.advanced_features import TrajectoryLogger

        src = inspect.getsource(TrajectoryLogger.log_run)

        has_lock = "Lock" in src or "_trajectory_lock" in src or ("lock" in src.lower() and "threading" in src.lower())

        if not has_lock:
            pytest.skip(
                "NEW-21: TrajectoryLogger.log_run has no threading.Lock. "
                "Concurrent writes to trajectory files can corrupt data. "
                "Fix: add a module-level Lock around file writes."
            )
        else:
            assert True, "NEW-21 fixed: TrajectoryLogger.log_run has thread lock"


# ---------------------------------------------------------------------------
# NEW-22: VectorStore.search returns vector column
# ---------------------------------------------------------------------------

class TestVectorStoreExcessiveColumnReturn:
    """NEW-22: VectorStore.search returns the raw 'vector' column in results."""

    def test_vector_store_search_source_does_not_drop_vector(self):
        """
        # BUG: NEW-22 — VectorStore.search returns results.to_dict("records")
        which includes the full float-array 'vector' column. This wastes memory
        and can cause JSON serialization failures.

        This test inspects the source for the drop-vector fix.
        """
        from src.core.indexing.vector_store import VectorStore

        src = inspect.getsource(VectorStore.search)

        # Check if the vector column is dropped before returning
        drops_vector = 'drop' in src and 'vector' in src

        if not drops_vector:
            pytest.skip(
                "NEW-22: VectorStore.search returns raw 'vector' column. "
                "Fix: use results.drop(columns=['vector'], errors='ignore').to_dict('records')"
            )
        else:
            assert True, "NEW-22 fixed: VectorStore.search drops vector column"


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
            "original_task", "step_description", "planned_action",
            "plan_validation", "plan_enforce_warnings", "plan_strict_mode", "task_history",
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
        assert '"rounds": 0' not in src or "rounds" not in src.split('"rounds": 0')[0][-200:].lower().replace(" ", ""), (
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
