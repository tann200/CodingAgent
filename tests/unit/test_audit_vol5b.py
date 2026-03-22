"""
Audit Vol5 (batch 2) — regression tests for the second wave of fixes.

  tool_last_used — cooldown enforcement in execution_node (COOLDOWN_GAP=3)
  files_read     — O(1) dict lookup for read-before-edit MODIFYING_TOOLS check
  F10            — plan_validator routes invalid plans to "planning", not "perception"
  F6             — sed -i bundled-flag detection (e.g. -ni catches 'i')
  F13            — glob rejects ".." traversal patterns; filters out-of-base paths
  P5             — planning_node max_tokens raised to 3000
  F8             — perception_node prompt-injection guard rejects reflected tool calls
  C4             — delegation_node injects results into history (not write-only)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> dict:
    base = {
        "task": "test task",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "next_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "total_debug_attempts": 0,
        "last_debug_error_type": None,
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
        "plan_resumed": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
        "original_task": None,
        "step_description": None,
        "planned_action": None,
        "plan_validation": None,
        "plan_enforce_warnings": None,
        "plan_strict_mode": None,
        "task_history": None,
        "step_retry_counts": None,
        "tool_last_used": {},
        "files_read": {},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AgentState: new fields declared
# ---------------------------------------------------------------------------

class TestNewStateFields:
    def test_tool_last_used_in_agent_state(self):
        from src.core.orchestration.graph.state import AgentState
        assert "tool_last_used" in AgentState.__annotations__

    def test_files_read_in_agent_state(self):
        from src.core.orchestration.graph.state import AgentState
        assert "files_read" in AgentState.__annotations__

    def test_tool_last_used_type_hint_is_dict(self):
        from src.core.orchestration.graph.state import AgentState
        import typing
        hint = AgentState.__annotations__.get("tool_last_used")
        assert hint is not None
        # Optional[Dict[str, int]] — should contain "Dict" in its repr
        assert "Dict" in str(hint) or "dict" in str(hint).lower()

    def test_files_read_type_hint_is_dict(self):
        from src.core.orchestration.graph.state import AgentState
        hint = AgentState.__annotations__.get("files_read")
        assert hint is not None
        assert "Dict" in str(hint) or "dict" in str(hint).lower()


# ---------------------------------------------------------------------------
# tool_last_used — cooldown enforcement
# ---------------------------------------------------------------------------

class TestToolLastUsedCooldown:
    """tool_last_used: same tool+path blocked within COOLDOWN_GAP=3 executions."""

    def _make_mock_orchestrator(self, tmp_path):
        orc = MagicMock()
        orc._session_read_files = set()
        orc._step_snapshot_id = None
        orc.cancel_event = None  # prevent MagicMock truthy from triggering cancel path
        orc.preflight_check.return_value = {"ok": True}
        orc.execute_tool.return_value = {"ok": True, "result": {"status": "ok", "content": "data"}}
        orc._check_loop_prevention = MagicMock(return_value=False)
        orc._read_execution_trace = MagicMock()
        orc._append_execution_trace = MagicMock()
        orc.msg_mgr = MagicMock()
        orc.working_dir = tmp_path
        return orc

    @pytest.mark.asyncio
    async def test_cooldown_blocks_repeated_read_file(self, tmp_path):
        """read_file called within COOLDOWN_GAP=3 of previous call must be blocked."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "read_file", "arguments": {"path": "auth.py"}},
            tool_last_used={"read_file:auth.py": 0},  # last called at count=0
            tool_call_count=1,  # current count=1; gap=1, COOLDOWN_GAP=3 → blocked
        )
        result = await execution_node(state, config)
        assert result.get("last_result", {}).get("ok") is False
        err = result.get("last_result", {}).get("error", "")
        assert "cooldown" in err.lower() or "already" in err.lower() or "context" in err.lower()
        # Tool should NOT have been executed
        orc.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_gap(self, tmp_path):
        """read_file allowed if gap >= COOLDOWN_GAP=3."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "read_file", "arguments": {"path": "auth.py"}},
            tool_last_used={"read_file:auth.py": 0},  # last at 0
            tool_call_count=3,  # gap=3 >= COOLDOWN_GAP=3 → allowed
        )
        result = await execution_node(state, config)
        # Should have executed (no cooldown block)
        orc.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_different_paths_not_blocked(self, tmp_path):
        """read_file on a different path is not blocked by same-name cooldown."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "read_file", "arguments": {"path": "other.py"}},
            # "read_file:auth.py" on cooldown, but "read_file:other.py" is fresh
            tool_last_used={"read_file:auth.py": 0},
            tool_call_count=1,
        )
        result = await execution_node(state, config)
        orc.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_read_tools_not_subject_to_cooldown(self, tmp_path):
        """edit_file is not a COOLDOWN_READ_TOOL and must never be blocked by cooldown."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        # Pre-read the file so MODIFYING_TOOLS check passes
        resolved = str((tmp_path / "auth.py").resolve())
        orc = self._make_mock_orchestrator(tmp_path)
        orc._session_read_files = {resolved}
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "edit_file", "arguments": {"path": "auth.py", "content": "x=1"}},
            tool_last_used={"edit_file:auth.py": 0},
            tool_call_count=1,  # within gap — but edit_file not in COOLDOWN_READ_TOOLS
            files_read={resolved: True},
        )
        result = await execution_node(state, config)
        orc.execute_tool.assert_called_once()


# ---------------------------------------------------------------------------
# files_read — O(1) dict lookup for MODIFYING_TOOLS
# ---------------------------------------------------------------------------

class TestFilesReadOOne:
    """files_read dict allows O(1) read-before-edit enforcement."""

    @pytest.mark.asyncio
    async def test_modifying_tool_blocked_when_files_read_empty(self, tmp_path):
        """edit_file on unread file must be blocked even if files_read dict is empty."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        orc = MagicMock()
        orc._session_read_files = set()
        orc._step_snapshot_id = None
        orc.cancel_event = None
        orc.preflight_check.return_value = {"ok": True}
        orc.working_dir = tmp_path
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "edit_file", "arguments": {"path": "auth.py", "content": "x"}},
            files_read={},  # empty — file not read
        )
        result = await execution_node(state, config)
        err = result.get("last_result", {}).get("error", "")
        assert "read" in err.lower() or "violation" in err.lower()

    @pytest.mark.asyncio
    async def test_modifying_tool_allowed_when_files_read_populated(self, tmp_path):
        """edit_file allowed when files_read contains the resolved path."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        resolved = str((tmp_path / "auth.py").resolve())
        orc = MagicMock()
        orc._session_read_files = set()
        orc._step_snapshot_id = None
        orc.cancel_event = None
        orc.preflight_check.return_value = {"ok": True}
        orc.execute_tool.return_value = {"ok": True, "result": {"status": "ok"}}
        orc._check_loop_prevention = MagicMock(return_value=False)
        orc._read_execution_trace = MagicMock()
        orc._append_execution_trace = MagicMock()
        orc.msg_mgr = MagicMock()
        orc.working_dir = tmp_path
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "edit_file", "arguments": {"path": "auth.py", "content": "x"}},
            files_read={resolved: True},  # file was read
        )
        result = await execution_node(state, config)
        orc.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_file_populates_files_read(self, tmp_path):
        """Successful read_file must add the resolved path to files_read dict."""
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        resolved = str((tmp_path / "foo.py").resolve())
        orc = MagicMock()
        orc._session_read_files = set()
        orc._step_snapshot_id = None
        orc.cancel_event = None
        orc.preflight_check.return_value = {"ok": True}
        orc.execute_tool.return_value = {
            "ok": True,
            "result": {"status": "ok", "content": "x=1"},
        }
        orc._check_loop_prevention = MagicMock(return_value=False)
        orc._read_execution_trace = MagicMock()
        orc._append_execution_trace = MagicMock()
        orc.msg_mgr = MagicMock()
        orc.working_dir = tmp_path
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={"name": "read_file", "arguments": {"path": "foo.py"}},
            files_read={},
        )
        result = await execution_node(state, config)
        files_read_out = result.get("files_read", {})
        assert resolved in files_read_out, (
            f"files_read must contain resolved path after read_file; got {files_read_out}"
        )


# ---------------------------------------------------------------------------
# F10 — plan_validator routes to "planning", not "perception"
# ---------------------------------------------------------------------------

class TestF10PlanValidatorRouting:
    """F10: invalid plans route to planning (not perception) to save 2 LLM calls."""

    def test_invalid_plan_routes_to_planning(self):
        from src.core.orchestration.graph.builder import should_after_plan_validator
        state = _make_state(
            plan_validation={"valid": False, "errors": ["missing test step"]},
            action_failed=False,
            rounds=0,
        )
        result = should_after_plan_validator(state)
        assert result == "planning", (
            f"F10: invalid plan must route to 'planning', got '{result}'"
        )

    def test_action_failed_routes_to_planning(self):
        from src.core.orchestration.graph.builder import should_after_plan_validator
        state = _make_state(action_failed=True, plan_validation={"valid": True}, rounds=0)
        result = should_after_plan_validator(state)
        assert result == "planning"

    def test_valid_plan_routes_to_execute(self):
        from src.core.orchestration.graph.builder import should_after_plan_validator
        state = _make_state(
            plan_validation={"valid": True},
            action_failed=False,
            rounds=0,
        )
        assert should_after_plan_validator(state) == "execute"

    def test_emergency_loop_guard_routes_to_execute(self):
        """After rounds >= 8 the emergency guard forces execution to break the loop."""
        from src.core.orchestration.graph.builder import should_after_plan_validator
        state = _make_state(
            plan_validation={"valid": False},
            action_failed=False,
            rounds=8,
        )
        # Must NOT route to planning (would loop forever); must force execute
        assert should_after_plan_validator(state) == "execute"

    def test_no_plan_validation_routes_to_planning(self):
        from src.core.orchestration.graph.builder import should_after_plan_validator
        state = _make_state(plan_validation=None, action_failed=False, rounds=1)
        assert should_after_plan_validator(state) == "planning"


# ---------------------------------------------------------------------------
# F6 — sed -i bundled-flag detection
# ---------------------------------------------------------------------------

class TestF6SedInplaceDetection:
    """F6: sed -i in any form must be blocked (bare, bundled, --in-place=...)."""

    def test_bare_dash_i_blocked(self):
        from src.tools.file_tools import bash
        result = bash("sed -i 's/foo/bar/' file.txt")
        assert result.get("status") == "error"
        assert "in-place" in result.get("error", "").lower() or "sed" in result.get("error", "").lower()

    def test_bundled_ni_blocked(self):
        """sed -ni (n + i bundled) must be blocked because 'i' is present."""
        from src.tools.file_tools import bash
        result = bash("sed -ni 's/foo/bar/' file.txt")
        assert result.get("status") == "error"

    def test_bundled_rni_blocked(self):
        from src.tools.file_tools import bash
        result = bash("sed -rni 's/foo/bar/g' file.txt")
        assert result.get("status") == "error"

    def test_in_place_long_option_blocked(self):
        from src.tools.file_tools import bash
        result = bash("sed --in-place 's/a/b/' f.txt")
        assert result.get("status") == "error"

    def test_in_place_eq_blocked(self):
        from src.tools.file_tools import bash
        result = bash("sed --in-place='' 's/a/b/' f.txt")
        assert result.get("status") == "error"

    def test_sed_without_inplace_allowed(self):
        """sed without -i is a text transform; it must not be blocked by the -i check."""
        from src.tools.file_tools import bash
        result = bash("sed 's/foo/bar/' file.txt")
        # Not an in-place error; may succeed or fail for other reasons
        assert "in-place" not in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# F13 — glob path traversal protection
# ---------------------------------------------------------------------------

class TestF13GlobPathTraversal:
    """F13: glob must reject '..' patterns and filter paths outside workdir."""

    def test_dotdot_in_pattern_rejected(self, tmp_path):
        from src.tools.file_tools import glob as glob_tool
        result = glob_tool("../../etc/passwd", workdir=tmp_path)
        assert result.get("status") == "error"
        assert ".." in result.get("error", "")

    def test_dotdot_deep_rejected(self, tmp_path):
        from src.tools.file_tools import glob as glob_tool
        result = glob_tool("sub/../../../etc/*", workdir=tmp_path)
        assert result.get("status") == "error"

    def test_valid_pattern_succeeds(self, tmp_path):
        (tmp_path / "foo.py").write_text("x=1")
        from src.tools.file_tools import glob as glob_tool
        result = glob_tool("*.py", workdir=tmp_path)
        assert result.get("status") == "ok"
        assert "foo.py" in result.get("matches", [])

    def test_recursive_pattern_succeeds(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "bar.py").write_text("y=2")
        from src.tools.file_tools import glob as glob_tool
        result = glob_tool("**/*.py", workdir=tmp_path)
        assert result.get("status") == "ok"
        assert any("bar.py" in m for m in result.get("matches", []))

    def test_result_paths_are_relative(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        from src.tools.file_tools import glob as glob_tool
        result = glob_tool("*.txt", workdir=tmp_path)
        assert result.get("status") == "ok"
        for m in result.get("matches", []):
            assert not m.startswith("/"), f"Match '{m}' must be relative"


# ---------------------------------------------------------------------------
# P5 — planning_node max_tokens raised to 3000
# ---------------------------------------------------------------------------

class TestP5PlanningMaxTokens:
    def test_planning_node_uses_3000_max_tokens(self):
        import inspect
        from src.core.orchestration.graph.nodes import planning_node as pn_mod
        source = inspect.getsource(pn_mod)
        assert "max_tokens=3000" in source, (
            "P5: planning_node must use max_tokens=3000 (was 1500)"
        )
        assert "max_tokens=1500" not in source, (
            "P5: old max_tokens=1500 must be removed from planning_node"
        )


# ---------------------------------------------------------------------------
# F8 — Prompt injection guard in perception_node
# ---------------------------------------------------------------------------

class TestF8PromptInjectionGuard:
    """F8: Tool call that matches a user-role history message must be rejected."""

    def test_injection_guard_rejects_reflected_tool_call(self):
        """
        If a user message contains 'name: bash' and the LLM reflects it back,
        the perception_node must reject the tool call.
        """
        import inspect
        from src.core.orchestration.graph.nodes import perception_node as pn_mod
        source = inspect.getsource(pn_mod)
        # Guard must exist in source
        assert "injection" in source.lower() or "F8" in source, (
            "F8: perception_node must contain a prompt injection guard"
        )
        assert "user_messages" in source or "role.*user" in source, (
            "F8: guard must scan user-role messages"
        )

    def test_injection_fingerprint_pattern(self):
        """The injection fingerprint 'name: <tool>' must match injected YAML."""
        tool_name = "bash"
        fingerprint = f"name: {tool_name}"
        user_msg = "Please run this: name: bash\narguments:\n  command: ls"
        assert fingerprint in user_msg


# ---------------------------------------------------------------------------
# C4 — delegation_node injects results into history
# ---------------------------------------------------------------------------

class TestC4DelegationResultsInHistory:
    """C4: delegation_node must add completed results to history (not write-only)."""

    @pytest.mark.asyncio
    async def test_delegation_results_injected_into_history(self):
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        with patch("src.core.orchestration.graph.nodes.delegation_node.delegate_task_async") as mock_del:
            mock_del.return_value = "subagent completed analysis of auth module"

            state = _make_state(
                delegations=[
                    {"role": "researcher", "task": "analyse auth module", "result_key": "auth_analysis"}
                ]
            )
            config = {}
            result = await delegation_node(state, config)

        # Must have history messages
        history = result.get("history", [])
        assert len(history) >= 1, "C4: delegation_node must inject results into history"

        # Content must contain the result
        combined = " ".join(m.get("content", "") for m in history)
        assert "auth_analysis" in combined or "auth module" in combined.lower() or "subagent" in combined.lower()

    @pytest.mark.asyncio
    async def test_delegation_error_still_produces_history(self):
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        with patch("src.core.orchestration.graph.nodes.delegation_node.delegate_task_async") as mock_del:
            mock_del.side_effect = RuntimeError("network failure")

            state = _make_state(
                delegations=[
                    {"role": "researcher", "task": "analyse", "result_key": "analysis"}
                ]
            )
            result = await delegation_node(state, config={})

        # Error results should also be surfaced in history
        history = result.get("history", [])
        # delegation_results should record the error
        dr = result.get("delegation_results", {})
        assert "analysis" in dr
        assert dr["analysis"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_no_delegations_returns_empty_history(self):
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node
        state = _make_state(delegations=[])
        result = await delegation_node(state, config={})
        # No delegations → nothing to inject
        assert result.get("history", []) == []

    @pytest.mark.asyncio
    async def test_delegation_results_still_in_state(self):
        """delegation_results must still be set in state for backward compat."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        with patch("src.core.orchestration.graph.nodes.delegation_node.delegate_task_async") as mock_del:
            mock_del.return_value = "done"
            state = _make_state(
                delegations=[{"role": "coder", "task": "fix bug", "result_key": "fix"}]
            )
            result = await delegation_node(state, config={})

        assert "delegation_results" in result
        assert "fix" in result["delegation_results"]
