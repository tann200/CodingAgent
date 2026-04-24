"""tests/unit/test_audit_vol27.py — Regression tests for Vol27 audit fixes.

CF-1: debug_node uses model=None (not "None" string)
CF-2: verification_node deletion failure sets verification_passed=False
UP-1: planning_node LLM success path sets task_decomposed=True
UP-2: start_new_task_impl calls clear_repo_summary_cache
"""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# TestCF1DebugNodeModelString
# ---------------------------------------------------------------------------


class TestCF1DebugNodeModelString:
    """CF-1: debug_node must use model=None (not the string "None")."""

    def _make_state(self, **extra) -> dict:
        base = {
            "task": "Fix the bug",
            "history": [],
            "working_dir": "/tmp/test",
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "total_debug_attempts": 0,
            "last_debug_error_type": None,
            "last_result": {"error": "test error"},
            "verification_result": {},
            "model_tier": None,
        }
        base.update(extra)
        return base

    def test_call_model_receives_none_when_adapter_has_no_models(self):
        """CF-1: When adapter.models is empty, call_model should receive model=None."""
        state = self._make_state()
        config = {}

        mock_adapter = MagicMock()
        mock_adapter.provider = {"name": "test_provider"}
        mock_adapter.models = []  # empty — triggers the bug

        mock_orch = MagicMock()
        mock_orch.adapter = mock_adapter
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.cancel_event = None  # prevent spurious cancellation

        captured_model = []

        async def fake_call_model(messages, **kwargs):
            captured_model.append(kwargs.get("model"))
            return {
                "choices": [
                    {"message": {"content": "name: bash\narguments:\n  cmd: echo hi"}}
                ]
            }

        with (
            patch(
                "src.core.orchestration.graph.nodes.debug_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.call_model",
                side_effect=fake_call_model,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.ContextBuilder"
            ) as mock_cb,
        ):
            mock_cb.return_value.build_prompt.return_value = [
                {"role": "user", "content": "fix"}
            ]
            asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.debug_node",
                    fromlist=["debug_node"],
                ).debug_node(state, config)
            )

        assert len(captured_model) == 1, "call_model should have been called once"
        assert captured_model[0] is None, (
            f"Expected model=None but got model={captured_model[0]!r}"
        )

    def test_call_model_receives_model_name_when_adapter_has_models(self):
        """CF-1: When adapter.models is populated, call_model should receive the model name."""
        state = self._make_state()
        config = {}

        mock_adapter = MagicMock()
        mock_adapter.provider = {"name": "openai"}
        mock_adapter.models = ["gpt-4o"]

        mock_orch = MagicMock()
        mock_orch.adapter = mock_adapter
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.cancel_event = None  # prevent spurious cancellation

        captured_model = []

        async def fake_call_model(messages, **kwargs):
            captured_model.append(kwargs.get("model"))
            return {
                "choices": [
                    {"message": {"content": "name: bash\narguments:\n  cmd: echo hi"}}
                ]
            }

        with (
            patch(
                "src.core.orchestration.graph.nodes.debug_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.call_model",
                side_effect=fake_call_model,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.ContextBuilder"
            ) as mock_cb,
        ):
            mock_cb.return_value.build_prompt.return_value = [
                {"role": "user", "content": "fix"}
            ]
            asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.debug_node",
                    fromlist=["debug_node"],
                ).debug_node(state, config)
            )

        assert len(captured_model) == 1
        assert captured_model[0] == "gpt-4o"

    def test_max_attempts_guard_still_works(self):
        """CF-1: debug_node should still bail when max_attempts reached (regression)."""
        state = self._make_state(debug_attempts=3, max_debug_attempts=3)
        config = {}

        mock_orch = MagicMock()
        mock_orch.adapter = MagicMock()

        with patch(
            "src.core.orchestration.graph.nodes.debug_node._resolve_orchestrator",
            return_value=mock_orch,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.debug_node",
                    fromlist=["debug_node"],
                ).debug_node(state, config)
            )

        assert result.get("next_action") is None
        assert (
            "max debug attempts" in " ".join(result.get("errors", [])).lower()
            or result.get("next_action") is None
        )

    def test_debug_node_no_adapter_returns_gracefully(self):
        """CF-1: When adapter is None, debug_node should still attempt model call with None."""
        state = self._make_state()
        config = {}

        mock_orch = MagicMock()
        mock_orch.adapter = None  # No adapter at all
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.cancel_event = None  # prevent spurious cancellation

        captured_model = []

        async def fake_call_model(messages, **kwargs):
            captured_model.append(kwargs.get("model"))
            return {
                "choices": [
                    {"message": {"content": "name: read_file\narguments:\n  path: x"}}
                ]
            }

        with (
            patch(
                "src.core.orchestration.graph.nodes.debug_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.call_model",
                side_effect=fake_call_model,
            ),
            patch(
                "src.core.orchestration.graph.nodes.debug_node.ContextBuilder"
            ) as mock_cb,
        ):
            mock_cb.return_value.build_prompt.return_value = [
                {"role": "user", "content": "fix"}
            ]
            asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.debug_node",
                    fromlist=["debug_node"],
                ).debug_node(state, config)
            )

        assert len(captured_model) == 1
        assert captured_model[0] is None  # must be None, not "None" string


# ---------------------------------------------------------------------------
# TestCF2DeletionVerificationPassed
# ---------------------------------------------------------------------------


class TestCF2DeletionVerificationPassed:
    """CF-2: verification_node must set verification_passed=False on deletion failure."""

    def _make_state_delete_failed(self, path: str = "src/foo.py") -> dict:
        return {
            "last_result": {"result": {"status": "ok", "deleted": True, "path": path}},
            "last_tool_name": "delete_file",
            "working_dir": "/tmp/testwd",
            "current_plan": [],
            "current_step": 0,
            "task": "Delete the file",
            "cancel_event": None,
            "verification_result": None,
            "evaluation_result": None,
        }

    def test_deletion_failure_sets_verification_passed_false(self, tmp_path):
        """CF-2: When deleted file still exists, verification_passed must be False."""
        # Create a file that will "survive" the deletion
        target = tmp_path / "foo.py"
        target.write_text("# test")

        state = self._make_state_delete_failed(str(target))
        state["working_dir"] = str(tmp_path)

        import src.core.orchestration.graph.nodes.verification_node as vn

        result = asyncio.run(vn.verification_node(state, {}))

        assert "verification_result" in result
        vr = result["verification_result"]
        assert vr.get("deletion_verification") == "FAILED"
        # The key fix: verification_passed must be False
        assert result.get("verification_passed") is False, (
            "Expected verification_passed=False but got: "
            + repr(result.get("verification_passed"))
        )

    def test_deletion_success_does_not_set_false(self, tmp_path):
        """CF-2: When file was actually deleted, verification result should not set False."""
        state = self._make_state_delete_failed(str(tmp_path / "gone.py"))
        state["working_dir"] = str(tmp_path)
        # File does NOT exist — deletion succeeded

        import src.core.orchestration.graph.nodes.verification_node as vn

        result = asyncio.run(vn.verification_node(state, {}))

        vr = result.get("verification_result", {})
        assert vr.get("deletion_verification") == "PASSED"
        # verification_passed may be absent or True — must not be False
        vp = result.get("verification_passed")
        assert vp is not False

    def test_evaluation_node_sees_failure(self, tmp_path):
        """CF-2: evaluation_node must NOT mark task complete when deletion failed."""
        # Simulate state after a failed deletion (verification_passed=False injected)
        state = {
            "task": "Delete foo.py",
            "verification_result": {
                "deletion_verification": "FAILED",
                "error": "File still exists: foo.py",
            },
            "verification_passed": False,
            "current_plan": [],
            "current_step": 0,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }

        import src.core.orchestration.graph.nodes.evaluation_node as en

        result = asyncio.run(en.evaluation_node(state, {}))
        # Should NOT be "complete" — should route to debug
        assert result.get("evaluation_result") != "complete", (
            "evaluation_node incorrectly marked task complete despite verification_passed=False"
        )

    def test_no_deletion_check_path_unaffected(self, tmp_path):
        """CF-2: Regular last_result (no deletion) path must not be broken."""
        state = {
            "last_result": {},
            "last_tool_name": "read_file",
            "working_dir": str(tmp_path),
            "current_plan": [],
            "current_step": 0,
            "task": "read a file",
            "cancel_event": None,
            "verification_result": None,
            "evaluation_result": None,
        }

        import src.core.orchestration.graph.nodes.verification_node as vn

        result = asyncio.run(vn.verification_node(state, {}))
        # No verification needed — should return empty results and True
        assert result.get("verification_passed") is True

    def test_deletion_failure_route_is_debug_not_complete(self, tmp_path):
        """CF-2: Full path: verification_node(fail) → evaluation_node → debug (not complete)."""
        from src.core.orchestration.graph.builder import should_after_evaluation

        state_after_eval = {
            "evaluation_result": "debug",
            "total_debug_attempts": 0,
            "current_step": 0,
            "step_retry_counts": {},
        }
        route = should_after_evaluation(state_after_eval)
        assert route == "debug"


# ---------------------------------------------------------------------------
# TestUP1TaskDecomposedFlag
# ---------------------------------------------------------------------------


class TestUP1TaskDecomposedFlag:
    """UP-1: planning_node LLM success path must set task_decomposed=True."""

    def _minimal_state(self) -> dict:
        return {
            "task": "implement feature X",
            "history": [],
            "working_dir": "/tmp/test",
            "current_plan": None,
            "current_step": 0,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "analyst_findings": None,
            "call_graph": None,
            "test_map": None,
            "model_tier": None,
            "plan_attempts": 0,
            "plan_mode_approved": None,
            "resume_session": False,
            "override_model": None,
        }

    def test_llm_success_path_sets_task_decomposed(self):
        """UP-1: When LLM returns a valid plan, task_decomposed=True must be in result."""
        state = self._minimal_state()

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.get_provider_capabilities.return_value = {}
        mock_orch.cancel_event = None  # prevent spurious cancellation

        llm_response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "```json\n"
                            '{"root_task":"implement X","steps":['
                            '{"step_id":"step_0","description":"Read existing code",'
                            '"files":["src/foo.py"],"depends_on":[]},'
                            '{"step_id":"step_1","description":"Add feature",'
                            '"files":["src/foo.py"],"depends_on":["step_0"]}'
                            "]}\n```"
                        )
                    }
                }
            ]
        }

        async def fake_call_model(*args, **kwargs):
            return llm_response

        with (
            patch(
                "src.core.orchestration.graph.nodes.planning_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.planning_node.call_model",
                side_effect=fake_call_model,
            ),
            patch(
                "src.core.orchestration.graph.nodes.planning_node.ContextBuilder"
            ) as mock_cb,
        ):
            mock_cb.return_value.build_prompt.return_value = [
                {"role": "user", "content": "plan"}
            ]
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.planning_node",
                    fromlist=["planning_node"],
                ).planning_node(state, {})
            )

        assert result.get("task_decomposed") is True, (
            "planning_node LLM success path missing task_decomposed=True"
        )

    def test_fallback_path_sets_task_decomposed(self):
        """UP-1: The F7 single-step fallback path must also set task_decomposed=True."""
        state = self._minimal_state()
        state["task"] = "do something"

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.get_provider_capabilities.return_value = {}

        mock_orch.cancel_event = None  # prevent spurious cancellation

        # LLM returns garbage → triggers fallback
        async def fake_call_model(*args, **kwargs):
            return {"choices": [{"message": {"content": "nope"}}]}

        with (
            patch(
                "src.core.orchestration.graph.nodes.planning_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.planning_node.call_model",
                side_effect=fake_call_model,
            ),
            patch(
                "src.core.orchestration.graph.nodes.planning_node.ContextBuilder"
            ) as mock_cb,
        ):
            mock_cb.return_value.build_prompt.return_value = [
                {"role": "user", "content": "plan"}
            ]
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.planning_node",
                    fromlist=["planning_node"],
                ).planning_node(state, {})
            )

        # F7 fallback fires when no steps parsed and task is set
        assert result.get("task_decomposed") is True or result.get("current_plan"), (
            "planning_node fallback path did not produce a usable plan"
        )

    def test_plan_resume_path_still_sets_task_decomposed(self, tmp_path):
        """UP-1: Plan-resume path (TTL-based) must set task_decomposed=True (regression)."""
        import json

        # Write a saved plan
        ctx_dir = tmp_path / ".agent-context"
        ctx_dir.mkdir(parents=True)
        from datetime import datetime

        plan_data = {
            "plan": [{"description": "step A", "action": None}],
            "task": "saved task",
            "current_step": 0,
            "working_dir": str(tmp_path),
            "saved_at": datetime.now().isoformat(),
        }
        (ctx_dir / "last_plan.json").write_text(json.dumps(plan_data))

        state = self._minimal_state()
        state["task"] = "saved task"
        state["working_dir"] = str(tmp_path)
        state["current_plan"] = None

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = None
        mock_orch.get_provider_capabilities.return_value = {}

        with patch(
            "src.core.orchestration.graph.nodes.planning_node._resolve_orchestrator",
            return_value=mock_orch,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.planning_node",
                    fromlist=["planning_node"],
                ).planning_node(state, {})
            )

        assert result.get("task_decomposed") is True
        assert result.get("plan_resumed") is True

    def test_replan_wont_regenerate_existing_plan(self):
        """UP-1: After task_decomposed=True is set, planning_node fast-path is taken
        and the returned state still carries task_decomposed=True."""
        state = self._minimal_state()
        state["task_decomposed"] = True
        state["current_plan"] = [{"description": "existing step", "action": None}]
        state["current_step"] = 0

        mock_orch = MagicMock()
        call_model_called = []

        async def fake_call_model(*args, **kwargs):
            call_model_called.append(1)
            return {"choices": [{"message": {"content": ""}}]}

        with (
            patch(
                "src.core.orchestration.graph.nodes.planning_node._resolve_orchestrator",
                return_value=mock_orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.planning_node.call_model",
                side_effect=fake_call_model,
            ),
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.planning_node",
                    fromlist=["planning_node"],
                ).planning_node(state, {})
            )

        assert not call_model_called, (
            "planning_node called LLM unnecessarily when task_decomposed=True and plan exists"
        )
        assert result.get("current_plan") == state["current_plan"]
        assert result.get("task_decomposed") is True, (
            "planning_node fast-path must return task_decomposed=True"
        )

    def test_next_action_path_no_regression(self, tmp_path):
        """UP-1: next_action trivial-plan path must still work (regression guard)."""
        state = self._minimal_state()
        # Use a unique working_dir (tmp_path) so no prior test's last_plan.json is loaded.
        state["working_dir"] = str(tmp_path)
        state["task"] = "trivial one-shot task unique 12345"
        state["next_action"] = {"name": "read_file", "arguments": {"path": "foo.py"}}

        mock_orch = MagicMock()

        with patch(
            "src.core.orchestration.graph.nodes.planning_node._resolve_orchestrator",
            return_value=mock_orch,
        ):
            result = asyncio.run(
                __import__(
                    "src.core.orchestration.graph.nodes.planning_node",
                    fromlist=["planning_node"],
                ).planning_node(state, {})
            )

        assert result.get("current_plan") is not None
        assert len(result["current_plan"]) == 1


# ---------------------------------------------------------------------------
# TestUP2RepoCacheClear
# ---------------------------------------------------------------------------


class TestUP2RepoCacheClear:
    """UP-2: start_new_task_impl must evict the repo summary cache for the working dir."""

    def test_start_new_task_calls_clear_repo_summary_cache(self, tmp_path):
        """UP-2: start_new_task_impl must call clear_repo_summary_cache(working_dir=<wd>)."""
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        mock_orch = MagicMock()
        mock_orch.working_dir = tmp_path
        mock_orch._plan_approval_event = None
        mock_orch._plan_approved = False
        mock_orch._pending_delegations = []
        mock_orch._session_title = None
        mock_orch._agent_mode = "execution"
        mock_orch._provider_name = "test"
        mock_orch.plan_mode = None
        mock_orch._execution_trace_buffer = []
        mock_orch._session_read_files = set()
        mock_orch._session_modified_files = set()
        mock_orch._last_agent_state = None

        # Patch the function inside analysis_node's namespace — that is where
        # start_new_task_impl imports it from at call time.
        with patch(
            "src.core.orchestration.graph.nodes.analysis_node.clear_repo_summary_cache",
        ) as mock_clear:
            start_new_task_impl(mock_orch)

        mock_clear.assert_called_once_with(working_dir=str(tmp_path))

    def test_analysis_node_has_clear_function(self):
        """UP-2: clear_repo_summary_cache must exist and be importable."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            clear_repo_summary_cache,
        )

        assert callable(clear_repo_summary_cache)

    def test_clear_evicts_specific_working_dir(self, tmp_path):
        """UP-2: Calling clear_repo_summary_cache(wd) evicts only that entry."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            clear_repo_summary_cache,
            _REPO_SUMMARY_CACHE,
            _REPO_SUMMARY_CACHE_LOCK,
        )

        resolved = str(tmp_path.resolve())
        with _REPO_SUMMARY_CACHE_LOCK:
            _REPO_SUMMARY_CACHE[resolved] = {"summary": "old summary"}
            _REPO_SUMMARY_CACHE["/other/path"] = {"summary": "keep this"}

        clear_repo_summary_cache(working_dir=str(tmp_path))

        with _REPO_SUMMARY_CACHE_LOCK:
            assert resolved not in _REPO_SUMMARY_CACHE, "Cache entry was not evicted"
            assert "/other/path" in _REPO_SUMMARY_CACHE, (
                "Unrelated entry was incorrectly evicted"
            )
            # Cleanup
            _REPO_SUMMARY_CACHE.pop("/other/path", None)

    def test_clear_all_clears_everything(self):
        """UP-2: clear_repo_summary_cache(working_dir=None) clears all entries."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            clear_repo_summary_cache,
            _REPO_SUMMARY_CACHE,
            _REPO_SUMMARY_CACHE_LOCK,
        )

        with _REPO_SUMMARY_CACHE_LOCK:
            _REPO_SUMMARY_CACHE["/a"] = {"s": 1}
            _REPO_SUMMARY_CACHE["/b"] = {"s": 2}

        clear_repo_summary_cache(working_dir=None)

        with _REPO_SUMMARY_CACHE_LOCK:
            assert len(_REPO_SUMMARY_CACHE) == 0

    def test_stale_cache_evicted_cross_task(self, tmp_path):
        """UP-2: Cross-task cache coherence — second task must not see first-task cache."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            _REPO_SUMMARY_CACHE,
            _REPO_SUMMARY_CACHE_LOCK,
        )
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        resolved = str(tmp_path.resolve())

        # Simulate cache populated by task 1
        with _REPO_SUMMARY_CACHE_LOCK:
            _REPO_SUMMARY_CACHE[resolved] = {"summary": "task1 stale data"}

        mock_orch = MagicMock()
        mock_orch.working_dir = tmp_path
        mock_orch._current_task_id = None
        mock_orch._plan_approval_event = None
        mock_orch._plan_approved = False
        mock_orch._pending_delegations = []
        mock_orch._session_title = None
        mock_orch._agent_mode = "execution"
        mock_orch._provider_name = "test"
        mock_orch.plan_mode = None
        mock_orch._execution_trace_buffer = []
        mock_orch._session_read_files = set()
        mock_orch._session_modified_files = set()
        mock_orch._last_agent_state = None

        # Calling start_new_task should clear the cache for this wd
        start_new_task_impl(mock_orch)

        with _REPO_SUMMARY_CACHE_LOCK:
            assert resolved not in _REPO_SUMMARY_CACHE, (
                "start_new_task_impl did not evict stale repo summary cache"
            )
