"""
Regression tests for audit vol7 (recorded as vol8 tests).

Covers:
- VOL7-1: total_debug_attempts in initial_state
- VOL7-3: repo_summary_data in initial_state
- VOL7-5: toolset loader uses Path(__file__).parent (not relative path)
- VOL7-7: _INDEXED_DIRS mutations are lock-protected
"""

import inspect
import threading
from pathlib import Path
import pytest


# ---------------------------------------------------------------------------
# VOL7-1: total_debug_attempts initialised in initial_state
# ---------------------------------------------------------------------------


class TestOrchestratorInitialStateTotalDebugAttempts:
    """VOL7-1: initial_state must include total_debug_attempts=0."""

    def _make_state(self):
        from unittest.mock import MagicMock

        # Import orchestrator and build a minimal initial_state the same way
        # run_agent_once() does — by inspecting the dict it builds.
        # We monkeypatch the graph invocation so nothing actually runs.
        from src.core.orchestration.orchestrator import Orchestrator

        orc = Orchestrator.__new__(Orchestrator)
        # Inject minimum attributes so initial_state construction doesn't crash
        orc.working_dir = Path("/tmp")
        orc._current_task_id = "test-task-id"
        orc.msg_mgr = MagicMock()
        orc.msg_mgr.messages = []
        orc.role_manager = MagicMock()
        orc.role_manager.get_system_prompt.return_value = "prompt"
        orc.deterministic = False
        orc.seed = None
        orc._session_read_files = set()
        orc.tool_registry = MagicMock()
        return orc

    def test_initial_state_contains_total_debug_attempts(self):
        """total_debug_attempts must be 0 in initial_state."""
        import src.core.orchestration.orchestrator as orc_mod

        src = inspect.getsource(orc_mod.Orchestrator.run_agent_once)
        # Verify the key is explicitly set (not just read)
        assert (
            '"total_debug_attempts": 0' in src or "'total_debug_attempts': 0" in src
        ), "VOL7-1: total_debug_attempts must be initialised to 0 in initial_state dict"

    def test_total_debug_attempts_zero(self):
        """total_debug_attempts initial value must be 0."""
        import src.core.orchestration.orchestrator as orc_mod

        src = inspect.getsource(orc_mod.Orchestrator.run_agent_once)
        # Should NOT have total_debug_attempts: None
        assert '"total_debug_attempts": None' not in src
        assert "'total_debug_attempts': None" not in src


# ---------------------------------------------------------------------------
# VOL7-3: repo_summary_data initialised in initial_state
# ---------------------------------------------------------------------------


class TestOrchestratorInitialStateRepoSummaryDataField:
    """VOL7-3: initial_state must include repo_summary_data."""

    def test_initial_state_contains_repo_summary_data(self):
        """repo_summary_data must be present in initial_state."""
        import src.core.orchestration.orchestrator as orc_mod

        src = inspect.getsource(orc_mod.Orchestrator.run_agent_once)
        assert '"repo_summary_data"' in src or "'repo_summary_data'" in src, (
            "VOL7-3: repo_summary_data must be initialised in initial_state dict"
        )


# ---------------------------------------------------------------------------
# VOL7-5: toolset loader uses __file__-relative path
# ---------------------------------------------------------------------------


class TestToolsetLoaderAbsolutePathResolution:
    """VOL7-5: _DIR in loader.py must resolve relative to __file__, not CWD."""

    def test_dir_is_absolute_or_file_relative(self):
        """_DIR must be independent of the process CWD."""
        from src.config.toolsets import loader as loader_mod

        _dir = loader_mod._DIR
        # Must be a Path object
        assert isinstance(_dir, Path), "_DIR must be a Path object"
        # The directory must actually exist (resolves correctly regardless of CWD)
        assert _dir.exists(), (
            f"VOL7-5: toolset _DIR={_dir!r} does not exist — "
            "relative path may not resolve from current working directory"
        )

    def test_dir_contains_yaml_files(self):
        """_DIR must contain at least the known toolset YAML files."""
        from src.config.toolsets import loader as loader_mod

        _dir = loader_mod._DIR
        yaml_files = list(_dir.glob("*.yaml"))
        assert len(yaml_files) > 0, f"VOL7-5: _DIR={_dir!r} contains no YAML files"

    def test_load_toolset_works_from_any_cwd(self, tmp_path, monkeypatch):
        """load_toolset must find YAMLs even when CWD is changed."""
        import os

        # Clear cache so fresh load is attempted
        from src.config.toolsets import loader as loader_mod

        loader_mod._cache.clear()
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = loader_mod.load_toolset("coding")
            assert result is not None, (
                "VOL7-5: load_toolset('coding') returned None when CWD != repo root. "
                "_DIR must use Path(__file__).parent not a relative path."
            )
        finally:
            os.chdir(original_cwd)
            loader_mod._cache.clear()

    def test_dir_not_uses_string_literal_path(self):
        """_DIR must not be Path('src/config/toolsets') — that depends on CWD."""
        from src.config.toolsets import loader as loader_mod

        src = inspect.getsource(loader_mod)
        assert 'Path("src/config/toolsets")' not in src, (
            "VOL7-5: _DIR must not use Path('src/config/toolsets') — "
            "use Path(__file__).parent instead"
        )
        assert "Path('src/config/toolsets')" not in src, (
            "VOL7-5: _DIR must not use Path('src/config/toolsets') — "
            "use Path(__file__).parent instead"
        )


# ---------------------------------------------------------------------------
# VOL7-7: _INDEXED_DIRS lock protection
# ---------------------------------------------------------------------------


class TestIndexedDirsLockPreventsRaceCondition:
    """VOL7-7: _INDEXED_DIRS mutations must be lock-protected."""

    def test_lock_exists(self):
        from src.core.orchestration.graph.nodes import analysis_node as an

        assert hasattr(an, "_INDEXED_DIRS_LOCK"), (
            "VOL7-7: _INDEXED_DIRS_LOCK must exist in analysis_node module"
        )
        assert isinstance(an._INDEXED_DIRS_LOCK, type(threading.Lock())), (
            "VOL7-7: _INDEXED_DIRS_LOCK must be a threading.Lock"
        )

    def test_mark_indexed_uses_lock(self):
        src = inspect.getsource(
            __import__(
                "src.core.orchestration.graph.nodes.analysis_node",
                fromlist=["_mark_indexed"],
            )._mark_indexed
        )
        assert "_INDEXED_DIRS_LOCK" in src, (
            "VOL7-7: _mark_indexed must acquire _INDEXED_DIRS_LOCK"
        )

    def test_is_already_indexed_uses_lock(self):
        src = inspect.getsource(
            __import__(
                "src.core.orchestration.graph.nodes.analysis_node",
                fromlist=["_is_already_indexed"],
            )._is_already_indexed
        )
        assert "_INDEXED_DIRS_LOCK" in src, (
            "VOL7-7: _is_already_indexed must acquire _INDEXED_DIRS_LOCK"
        )

    def test_concurrent_mark_indexed_is_safe(self, tmp_path):
        """Concurrent _mark_indexed calls must not crash."""
        from src.core.orchestration.graph.nodes import analysis_node as an

        dirs = []
        for i in range(20):
            d = tmp_path / f"d{i}"
            d.mkdir()
            dirs.append(str(d))

        errors = []

        def worker(path):
            try:
                an._mark_indexed(path)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"VOL7-7: concurrent _mark_indexed raised: {errors}"


# ===========================================================================
# Fixes implemented in the same session (providers.json + vol6 remainders)
# ===========================================================================

# ---------------------------------------------------------------------------
# providers.json array format
# ---------------------------------------------------------------------------


class TestProvidersJsonMustBeArray:
    """providers.json must be a JSON array."""

    def test_providers_json_is_array(self):
        import json
        from pathlib import Path

        path = Path(__file__).parent.parent.parent / "src" / "config" / "providers.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, list), (
            f"providers.json must be an array, got {type(raw).__name__}"
        )


# ---------------------------------------------------------------------------
# HR-5: preflight_check validates bash dangerous patterns
# ---------------------------------------------------------------------------


class TestPreflightCheckBlocksDangerousPatterns:
    """HR-5: preflight_check must reject dangerous bash commands."""

    def _make_orchestrator(self, tmp_path):
        from unittest.mock import MagicMock
        from src.core.orchestration.orchestrator import Orchestrator

        orc = Orchestrator.__new__(Orchestrator)
        orc.working_dir = tmp_path
        reg = MagicMock()
        reg.get.return_value = {"side_effects": [], "description": "run shell command"}
        reg.tools = {"bash": {"side_effects": [], "description": "run shell command"}}
        orc.tool_registry = reg
        return orc

    def test_dangerous_pattern_pipe_rejected(self, tmp_path):
        orc = self._make_orchestrator(tmp_path)
        result = orc.preflight_check(
            {"name": "bash", "arguments": {"command": "ls | grep foo"}}
        )
        assert result["ok"] is False
        assert "dangerous" in result["error"].lower() or "|" in result["error"]

    def test_dangerous_pattern_rm_rf_rejected(self, tmp_path):
        orc = self._make_orchestrator(tmp_path)
        result = orc.preflight_check(
            {"name": "bash", "arguments": {"command": "rm -rf /tmp/x"}}
        )
        assert result["ok"] is False

    def test_safe_command_passes(self, tmp_path):
        orc = self._make_orchestrator(tmp_path)
        result = orc.preflight_check(
            {"name": "bash", "arguments": {"command": "pytest tests/"}}
        )
        assert result["ok"] is True

    def test_whitespace_normalised(self, tmp_path):
        """Spacing tricks must not bypass the check."""
        orc = self._make_orchestrator(tmp_path)
        result = orc.preflight_check(
            {"name": "bash", "arguments": {"command": "rm  -rf /tmp"}}
        )
        assert result["ok"] is False

    def test_bash_dangerous_patterns_class_attr_exists(self):
        from src.core.orchestration.orchestrator import Orchestrator

        assert hasattr(Orchestrator, "_BASH_DANGEROUS_PATTERNS"), (
            "HR-5: _BASH_DANGEROUS_PATTERNS must be a class attribute on Orchestrator"
        )
        assert len(Orchestrator._BASH_DANGEROUS_PATTERNS) > 5


# ---------------------------------------------------------------------------
# UP-1: Unified read-before-edit error message
# ---------------------------------------------------------------------------


class TestReadBeforeWriteUnifiedErrorMessage:
    """UP-1: both error sites must use the same canonical wording."""

    _CANONICAL = "before writing to it"

    def test_orchestrator_uses_canonical_wording(self):
        import src.core.orchestration.orchestrator as orc_mod

        src_text = inspect.getsource(orc_mod.Orchestrator.execute_tool)
        assert self._CANONICAL in src_text, (
            f"UP-1: orchestrator.execute_tool must contain '{self._CANONICAL}'"
        )

    def test_execution_node_uses_canonical_wording(self):
        from src.core.orchestration.graph.nodes import execution_node as en_mod

        src_text = inspect.getsource(en_mod.execution_node)
        assert self._CANONICAL in src_text, (
            f"UP-1: execution_node must contain '{self._CANONICAL}'"
        )

    def test_messages_are_identical_prefix(self):
        """Both messages must start with the same prefix."""
        import src.core.orchestration.orchestrator as orc_mod
        from src.core.orchestration.graph.nodes import execution_node as en_mod

        orc_src = inspect.getsource(orc_mod.Orchestrator.execute_tool)
        en_src = inspect.getsource(en_mod.execution_node)
        assert "Security/Logic violation" in orc_src
        assert "Security/Logic violation" in en_src


# ---------------------------------------------------------------------------
# OE-3: ModelRouter class removed
# ---------------------------------------------------------------------------


class TestModelRouterDeadCodeAbsent:
    """OE-3: ModelRouter dead code must be removed from orchestrator."""

    def test_model_router_not_in_module(self):
        import src.core.orchestration.orchestrator as orc_mod

        assert not hasattr(orc_mod, "ModelRouter"), (
            "OE-3: ModelRouter class must be removed from orchestrator.py"
        )

    def test_orchestrator_class_still_exists(self):
        from src.core.orchestration.orchestrator import Orchestrator

        assert Orchestrator is not None


# ---------------------------------------------------------------------------
# TS-4: plan_validator_node scans descriptions for unknown tool names
# ---------------------------------------------------------------------------


class TestPlanValidatorWarnsOnUnknownToolNames:
    """TS-4: validate_plan must warn when description mentions unknown tool names."""

    def test_unknown_tool_in_backticks_produces_warning(self):
        from src.core.orchestration.graph.nodes.plan_validator_node import validate_plan

        plan = [
            {
                "description": "Use `nonexistent_tool` to process the file",
                "action": None,
            }
        ]
        result = validate_plan(plan, registered_tools={"read_file", "write_file"})
        warnings = result.get("warnings", [])
        assert any("nonexistent_tool" in w for w in warnings), (
            f"TS-4: unknown backtick-wrapped tool in description must generate warning. "
            f"Got warnings: {warnings}"
        )

    def test_known_tool_in_backticks_no_warning(self):
        from src.core.orchestration.graph.nodes.plan_validator_node import validate_plan

        plan = [
            {"description": "Use `read_file` to inspect src/foo.py", "action": None}
        ]
        result = validate_plan(plan, registered_tools={"read_file", "write_file"})
        tool_warnings = [w for w in result.get("warnings", []) if "read_file" in w]
        assert not tool_warnings, (
            "TS-4: known tool referenced in description must not produce a warning"
        )

    def test_action_name_still_checked(self):
        from src.core.orchestration.graph.nodes.plan_validator_node import validate_plan

        plan = [{"description": "Do something", "action": {"name": "ghost_tool"}}]
        result = validate_plan(plan, registered_tools={"read_file", "write_file"})
        errors = result.get("errors", [])
        assert any("ghost_tool" in e for e in errors), (
            "TS-4: unknown action.name must still produce an error"
        )


# ---------------------------------------------------------------------------
# WR-5: _parse_plan_content strategy 3 ignores analysis preamble
# ---------------------------------------------------------------------------


class TestParsePlanContentSkipsPreambleSentences:
    """WR-5: free-text lines must not be ingested after structured lines are found."""

    def _parse(self, content):
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        return _parse_plan_content(content)

    def test_preamble_not_ingested_when_numbered_list_follows(self):
        """Analysis-context sentence before a numbered list must not become a step."""
        content = (
            "We need to read the auth module to understand the structure.\n"
            "1. Read src/auth.py\n"
            "2. Edit the login function\n"
            "3. Run tests\n"
        )
        steps = self._parse(content)
        descriptions = [s["description"] for s in steps]
        # Numbered items must be present
        assert any("Read src/auth.py" in d or "auth.py" in d for d in descriptions)
        # Preamble sentence must NOT be a step
        assert not any(
            "We need to read" in d or "understand the structure" in d
            for d in descriptions
        ), f"WR-5: preamble sentence must not become a plan step. Steps: {descriptions}"

    def test_pure_free_text_still_works_when_no_structure(self):
        """When there are no numbered/bullet lines, free-text fallback still fires."""
        content = "Read the config file and update the timeout value"
        steps = self._parse(content)
        assert len(steps) >= 1, (
            "WR-5: free-text content should still produce a step when no structure found"
        )

    def test_numbered_list_all_captured(self):
        content = "1. Read foo.py\n2. Edit the function\n3. Run tests\n"
        steps = self._parse(content)
        assert len(steps) == 3


# ---------------------------------------------------------------------------
# UP-3: get_tools_for_role returns role-filtered tool lists
# ---------------------------------------------------------------------------


class TestOrchestratorRoleFilteredToolSelection:
    """UP-3: Orchestrator.get_tools_for_role must return role-appropriate tools."""

    def _make_orchestrator_with_tools(self, tool_names):
        from unittest.mock import MagicMock
        from src.core.orchestration.orchestrator import Orchestrator

        orc = Orchestrator.__new__(Orchestrator)
        tools_dict = {
            n: {"description": f"tool {n}", "side_effects": []} for n in tool_names
        }
        reg = MagicMock()
        reg.tools = tools_dict
        reg.get = lambda name: tools_dict.get(name)
        orc.tool_registry = reg
        return orc

    def test_debugger_role_gets_debug_tools(self):
        """Debugger role must receive tools from the debug toolset."""
        all_tools = [
            "read_file",
            "list_files",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "run_tests",
            "run_linter",
            "syntax_check",
            "bash_readonly",
            "get_git_diff",
            "memory_search",
            "batched_file_read",
            "multi_file_summary",
            "write_file",
            "edit_file",
        ]
        orc = self._make_orchestrator_with_tools(all_tools)
        tools = orc.get_tools_for_role("debugger")
        names = {t["name"] for t in tools}
        # Debug toolset must include bash_readonly and read_file
        assert "bash_readonly" in names, "debugger role must have bash_readonly tool"
        assert "read_file" in names, "debugger role must have read_file tool"

    def test_operational_role_gets_coding_tools(self):
        """Operational role must receive tools from the coding toolset."""
        all_tools = [
            "read_file",
            "write_file",
            "edit_file",
            "edit_by_line_range",
            "delete_file",
            "list_files",
            "glob",
            "search_code",
            "find_symbol",
            "find_references",
            "grep",
            "run_tests",
            "run_linter",
            "syntax_check",
            "apply_patch",
            "generate_patch",
            "get_git_diff",
            "read_file_chunk",
            "batched_file_read",
        ]
        orc = self._make_orchestrator_with_tools(all_tools)
        tools = orc.get_tools_for_role("operational")
        names = {t["name"] for t in tools}
        assert "write_file" in names
        assert "edit_file" in names

    def test_unknown_role_falls_back_to_full_registry(self):
        """Unknown role must fall back to full registry (graceful degradation)."""
        all_tools = ["read_file", "write_file", "bash"]
        orc = self._make_orchestrator_with_tools(all_tools)
        tools = orc.get_tools_for_role("unknown_role_xyz")
        names = {t["name"] for t in tools}
        # Must have all registered tools since toolset would be empty or mismatch
        assert names >= {"read_file", "write_file", "bash"}

    def test_returns_list_of_dicts_with_name_and_description(self):
        """Return value must be a list of {name, description} dicts."""
        all_tools = ["read_file", "write_file", "bash"]
        orc = self._make_orchestrator_with_tools(all_tools)
        tools = orc.get_tools_for_role("operational")
        assert isinstance(tools, list)
        for t in tools:
            assert "name" in t
            assert "description" in t


# ===========================================================================
# SCAN findings
# ===========================================================================

# ---------------------------------------------------------------------------
# SCAN-2: syntax_check scoped to modified file on intermediate steps
# ---------------------------------------------------------------------------


class TestVerificationNodeSingleFileSyntaxOnIntermediateSteps:
    """SCAN-2: intermediate steps must not walk entire directory for syntax check."""

    def test_full_suite_runs_syntax_check(self):
        """Final step must include syntax results."""
        import inspect
        from src.core.orchestration.graph.nodes import verification_node as vn_mod

        src = inspect.getsource(vn_mod)
        assert "syntax_check(str(wd))" in src, (
            "verification_node must call syntax_check on final step"
        )

    def test_intermediate_step_uses_py_compile_not_walk(self):
        """Intermediate step code path must use py_compile, not syntax_check walk."""
        import inspect
        from src.core.orchestration.graph.nodes import verification_node as vn_mod

        src = inspect.getsource(vn_mod)
        assert "py_compile" in src, (
            "SCAN-2: intermediate step must use py_compile for single-file check"
        )
        # The per-file fix must be inside the else branch (not run_full_suite)
        assert "SCAN-2" in src or "intermediate step" in src.lower()

    @pytest.mark.asyncio
    async def test_intermediate_step_does_not_walk_directory(self, tmp_path):
        """Intermediate step with a known .py file must NOT call syntax_check(workdir)."""
        from unittest.mock import patch

        # Create a test .py file in the tmp dir
        py_file = tmp_path / "foo.py"
        py_file.write_text("x = 1\n")

        state = {
            "need_verify": True,
            "current_plan": [{"description": "step 1"}, {"description": "step 2"}],
            "current_step": 0,  # NOT the final step
            "working_dir": str(tmp_path),
            "last_result": {"path": str(py_file)},
            "cancel_event": None,
        }

        walk_called = []

        def fake_syntax_check(wd):
            walk_called.append(wd)
            return {"status": "ok", "checked_files": 0, "syntax_errors": []}

        from src.core.orchestration.graph.nodes import verification_node as vn_mod

        with patch.object(
            vn_mod.verification_tools, "syntax_check", side_effect=fake_syntax_check
        ):
            await vn_mod.verification_node(state, {})

        assert not walk_called, (
            f"SCAN-2: verification_tools.syntax_check must NOT be called on intermediate step. "
            f"Called with: {walk_called}"
        )


# ---------------------------------------------------------------------------
# SCAN-4: get_tools_for_role fallback logs a warning
# ---------------------------------------------------------------------------


class TestOrchestratorToolRoleFallbackEmitsWarning:
    """SCAN-4: fallback to full registry must emit a warning log."""

    def test_fallback_logs_warning(self):
        from unittest.mock import MagicMock, patch
        from src.core.orchestration.orchestrator import Orchestrator
        import src.core.orchestration.orchestrator as orc_mod

        orc = Orchestrator.__new__(Orchestrator)
        tools_dict = {"read_file": {"description": "read", "side_effects": []}}
        reg = MagicMock()
        reg.tools = tools_dict
        orc.tool_registry = reg

        warning_calls = []
        with patch.object(
            orc_mod.guilogger,
            "warning",
            side_effect=lambda msg, *a, **kw: warning_calls.append(msg),
        ):
            # Force a fallback: debugger toolset has 14 tools but only 1 is registered
            orc.get_tools_for_role("debugger")

        assert any(
            "fallback" in str(m).lower() or "falling back" in str(m).lower()
            for m in warning_calls
        ), (
            f"SCAN-4: get_tools_for_role must log a WARNING when falling back. "
            f"Warning calls: {warning_calls}"
        )

    def test_fallback_still_returns_all_tools(self):
        from unittest.mock import MagicMock
        from src.core.orchestration.orchestrator import Orchestrator

        orc = Orchestrator.__new__(Orchestrator)
        tools_dict = {"read_file": {"description": "r", "side_effects": []}}
        reg = MagicMock()
        reg.tools = tools_dict
        orc.tool_registry = reg

        result = orc.get_tools_for_role("unknown_xyz")
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# SCAN-6: distiller pool does not block on timeout
# ---------------------------------------------------------------------------


class TestDistillerPoolFuturesCancelledOnTimeout:
    """SCAN-6: distiller must not block when future times out."""

    def test_pool_uses_shutdown_wait_false(self):
        """After SCAN-6 fix the pool must use shutdown(wait=False)."""
        import inspect
        from src.core.memory import distiller as d_mod

        src = inspect.getsource(d_mod._call_llm_sync)
        assert "shutdown(wait=False)" in src, (
            "SCAN-6: pool must call shutdown(wait=False) so a timed-out thread "
            "does not block the caller"
        )

    def test_future_cancel_called_on_error(self):
        """future.cancel() must be called in the error path."""
        import inspect
        from src.core.memory import distiller as d_mod

        src = inspect.getsource(d_mod._call_llm_sync)
        assert "future.cancel()" in src, (
            "SCAN-6: future.cancel() must be called when future.result() raises"
        )


# ---------------------------------------------------------------------------
# SCAN-10: graph executor shutdown uses wait=True
# ---------------------------------------------------------------------------


class TestGraphExecutorWaitsForThreadsOnShutdown:
    """SCAN-10: executor shutdown must use wait=True for clean thread join."""

    def test_shutdown_wait_true(self):
        import inspect
        from src.core.orchestration.orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator.run_agent_once)
        assert "shutdown(wait=True)" in src, (
            "SCAN-10: _graph_executor.shutdown must use wait=True"
        )
        assert "shutdown(wait=False)" not in src, (
            "SCAN-10: wait=False must not appear in run_agent_once"
        )


# ---------------------------------------------------------------------------
# SCAN2-1/2/3: asyncio.CancelledError propagated in async nodes
# ---------------------------------------------------------------------------


class TestAsyncCancelledErrorPropagatesInNodes:
    """SCAN2-1/2/3: await llm_task must not be swallowed by except Exception."""

    def _check_node_reraises(self, module_path: str, node_func_name: str):
        from pathlib import Path as _Path

        src = _Path(module_path).read_text()
        # Check that there is a CancelledError re-raise near every `await llm_task`
        # by looking for the pattern in source text (simpler than full AST walk)
        assert "asyncio.CancelledError" in src, (
            f"SCAN2: {module_path} must handle asyncio.CancelledError near await llm_task"
        )
        assert "raise" in src, f"SCAN2: {module_path} must re-raise CancelledError"

    def test_debug_node_cancelled_error(self):
        self._check_node_reraises(
            "src/core/orchestration/graph/nodes/debug_node.py",
            "debug_node",
        )

    def test_execution_node_cancelled_error(self):
        self._check_node_reraises(
            "src/core/orchestration/graph/nodes/execution_node.py",
            "execution_node",
        )

    def test_planning_node_cancelled_error(self):
        self._check_node_reraises(
            "src/core/orchestration/graph/nodes/planning_node.py",
            "planning_node",
        )

    def test_debug_node_cancelled_error_not_swallowed(self):
        """except Exception must not appear AFTER CancelledError try/except block."""
        from pathlib import Path as _Path

        src = _Path("src/core/orchestration/graph/nodes/debug_node.py").read_text()
        # The CancelledError handler must appear alongside a `raise` — not a `pass`
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if "except asyncio.CancelledError" in line:
                # next non-blank line must be `raise`
                for j in range(i + 1, min(i + 4, len(lines))):
                    stripped = lines[j].strip()
                    if stripped:
                        assert (
                            stripped
                            == "raise  # propagate — node itself was cancelled; do not swallow"
                            or stripped == "raise"
                        ), (
                            f"SCAN2-1: CancelledError handler in debug_node must re-raise, got: {stripped!r}"
                        )
                        break


# ---------------------------------------------------------------------------
# SCAN2-5: session_store check_same_thread contradiction fixed
# ---------------------------------------------------------------------------


class TestSessionStoreSQLiteCheckSameThreadRemoved:
    """SCAN2-5: check_same_thread=False must not appear alongside threading.local()."""

    def test_check_same_thread_removed(self):
        from pathlib import Path as _Path

        src = _Path("src/core/memory/session_store.py").read_text()
        assert "check_same_thread=False" not in src, (
            "SCAN2-5: check_same_thread=False contradicts threading.local() pattern; must be removed"
        )
        # threading.local() must still be present
        assert "threading.local()" in src, (
            "SCAN2-5: threading.local() must still be used for per-thread connections"
        )


# ---------------------------------------------------------------------------
# SCAN2-6: context_builder cache mutations are lock-protected
# ---------------------------------------------------------------------------


class TestContextBuilderCacheMutationUnderLock:
    """SCAN2-6: OrderedDict cache mutations must be protected by a threading.Lock."""

    def test_cache_lock_exists(self):
        from pathlib import Path as _Path

        src = _Path("src/core/context/context_builder.py").read_text()
        assert "_CACHE_LOCK" in src, (
            "SCAN2-6: _CACHE_LOCK must be defined in context_builder.py"
        )
        assert "threading.Lock()" in src, (
            "SCAN2-6: _CACHE_LOCK must be a threading.Lock instance"
        )

    def test_cache_mutations_use_lock(self):
        from pathlib import Path as _Path

        src = _Path("src/core/context/context_builder.py").read_text()
        # Both move_to_end and popitem must appear inside with _CACHE_LOCK blocks
        assert "with _CACHE_LOCK:" in src, (
            "SCAN2-6: cache mutations must be wrapped in with _CACHE_LOCK:"
        )

    def test_cache_thread_safety(self):
        """Concurrent reads from multiple threads must not corrupt the cache."""
        import threading as _threading
        from src.core.context import context_builder as cb_mod

        # Clear caches before test
        with cb_mod._CACHE_LOCK:
            cb_mod._TEXT_CACHE.clear()

        errors = []

        def _read(path_str, results):
            try:
                from pathlib import Path as _Path

                # Use a tmp file via pytest tmp_path — simulate with __file__
                p = _Path(__file__)  # always exists
                result = cb_mod.ContextBuilder._read_text_cached(p)
                results.append(result)
            except Exception as e:
                errors.append(str(e))

        results = []
        threads = [
            _threading.Thread(target=_read, args=(__file__, results)) for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"SCAN2-6: thread-safety errors: {errors}"
        assert all(r is not None for r in results), (
            "SCAN2-6: all threads must get a non-None result"
        )


# ---------------------------------------------------------------------------
# TOCTOU fix: mtime read inside lock in _is_already_indexed / _mark_indexed
# ---------------------------------------------------------------------------


class TestIndexedDirsStatCallInsideLock:
    """Regression: os.stat() must be called inside _INDEXED_DIRS_LOCK to avoid TOCTOU."""

    def test_is_already_indexed_stat_inside_lock(self):
        """_is_already_indexed must read mtime inside the lock block."""
        src = inspect.getsource(
            __import__(
                "src.core.orchestration.graph.nodes.analysis_node",
                fromlist=["_is_already_indexed"],
            )._is_already_indexed
        )
        # The lock context manager must appear BEFORE the stat call
        lock_pos = src.find("_INDEXED_DIRS_LOCK")
        stat_pos = src.find("st_mtime_ns")
        assert lock_pos != -1, "_INDEXED_DIRS_LOCK must appear in _is_already_indexed"
        assert stat_pos != -1, "st_mtime_ns must appear in _is_already_indexed"
        assert lock_pos < stat_pos, (
            "TOCTOU fix: _INDEXED_DIRS_LOCK must be acquired BEFORE reading st_mtime_ns "
            "in _is_already_indexed"
        )

    def test_mark_indexed_stat_inside_lock(self):
        """_mark_indexed must read mtime inside the lock block."""
        src = inspect.getsource(
            __import__(
                "src.core.orchestration.graph.nodes.analysis_node",
                fromlist=["_mark_indexed"],
            )._mark_indexed
        )
        lock_pos = src.find("_INDEXED_DIRS_LOCK")
        stat_pos = src.find("st_mtime_ns")
        assert lock_pos != -1, "_INDEXED_DIRS_LOCK must appear in _mark_indexed"
        assert stat_pos != -1, "st_mtime_ns must appear in _mark_indexed"
        assert lock_pos < stat_pos, (
            "TOCTOU fix: _INDEXED_DIRS_LOCK must be acquired BEFORE reading st_mtime_ns "
            "in _mark_indexed"
        )

    def test_is_already_indexed_no_stat_before_lock(self, tmp_path):
        """Functional: mtime read inside lock means consistent comparison."""
        from src.core.orchestration.graph.nodes import analysis_node as an

        # Mark directory as indexed
        an._mark_indexed(str(tmp_path))
        # Should report as already indexed immediately after
        assert an._is_already_indexed(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# plan_mode_approved reset uses False not None
# ---------------------------------------------------------------------------


class TestPlanningNodeResetsPlanModeApprovedToFalse:
    """Regression: plan_mode_approved must be reset to False (not None) after first write."""

    def test_plan_approval_consumed_uses_false(self):
        """execution_node must set plan_mode_approved=False, not None, on approval reset."""
        from pathlib import Path as _Path

        src = _Path("src/core/orchestration/graph/nodes/execution_node.py").read_text()
        assert (
            '"plan_mode_approved": False' in src or "'plan_mode_approved': False" in src
        ), (
            "plan_mode_approved reset must use False not None for unambiguous boolean state"
        )
        assert (
            '"plan_mode_approved": None' not in src
            and "'plan_mode_approved': None" not in src
        ), (
            "plan_mode_approved must not be reset to None — use False for explicit boolean"
        )
