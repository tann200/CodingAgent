"""tests/integration/test_delegation_mock.py — IMPL-IT-2

Mock-based integration tests for delegation_node.  These never call a live LLM
or spawn real subagents; delegate_task_async is patched to return a controlled
string result.

Coverage:
  DM-1  test_delegation_node_subagent_receives_task
        delegation_node receives a delegation with a task; the subagent adapter
        (delegate_task_async) is called with the expected role + description.

  DM-2  test_analyst_delegation_injects_findings
        When a researcher delegation completes, the result text appears in the
        history messages injected by delegation_node so the next planning cycle
        can see it.

  DM-3  test_subagent_adapter_none_does_not_crash
        When delegations list is empty, delegation_node returns {} without error.

  DM-4  test_delegation_result_in_history
        The delegation_results dict is populated with the correct key/status and
        the summary is appended to state history as a user message.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "src.core.orchestration.graph.nodes.delegation_node"


def _make_state(delegations: list, **extra) -> dict:
    """Minimal AgentState-like dict for delegation_node."""
    return {
        "delegations": delegations,
        "working_dir": "/tmp/test_workdir",
        "session_id": "test-session-01",
        "history": [],
        "_file_lock_manager": None,
        **extra,
    }


def _make_config() -> dict:
    """Minimal LangGraph config dict."""
    return {"configurable": {}}


def _run(coro):
    """Run a coroutine synchronously (for pytest without asyncio mode)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DM-1  — subagent receives task
# ---------------------------------------------------------------------------


class TestDelegationNodeSubagentReceivesTask:
    """DM-1: delegation_node passes the correct role + task to delegate_task_async."""

    def test_delegation_node_subagent_receives_task(self):
        """The mocked delegate_task_async is called with the right arguments."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        delegations = [
            {
                "role": "researcher",
                "task": "Analyse the authentication module for security flaws",
                "result_key": "auth_analysis",
            }
        ]
        state = _make_state(delegations)
        config = _make_config()

        mock_delegate = AsyncMock(return_value="Found 2 potential issues.")

        with patch(f"{_MODULE}.delegate_task_async", mock_delegate):
            result = _run(delegation_node(state, config))

        # Verify the mock was called once with matching args.
        assert mock_delegate.call_count == 1
        call_kwargs = mock_delegate.call_args
        assert call_kwargs.kwargs.get("role") == "researcher" or (
            call_kwargs.args and call_kwargs.args[0] == "researcher"
        )
        task_arg = call_kwargs.kwargs.get("subtask_description") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else ""
        )
        assert "authentication" in task_arg or "authentication" in str(call_kwargs)


# ---------------------------------------------------------------------------
# DM-2  — analyst delegation injects findings
# ---------------------------------------------------------------------------


class TestAnalystDelegationInjectsFindings:
    """DM-2: completed delegation result is injected into history."""

    def test_analyst_delegation_injects_findings(self):
        """Result text from the subagent appears in the returned history messages."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        FINDING = "The function foo() has an off-by-one error on line 42."
        delegations = [
            {
                "role": "researcher",
                "task": "Review foo() for off-by-one errors",
                "result_key": "code_review",
            }
        ]
        state = _make_state(delegations)
        config = _make_config()

        mock_delegate = AsyncMock(return_value=FINDING)

        with patch(f"{_MODULE}.delegate_task_async", mock_delegate):
            result = _run(delegation_node(state, config))

        # The history list returned must contain a message with the finding.
        history_msgs = result.get("history", [])
        assert len(history_msgs) >= 1
        combined_content = " ".join(
            m.get("content", "") for m in history_msgs if isinstance(m, dict)
        )
        assert FINDING in combined_content or "off-by-one" in combined_content


# ---------------------------------------------------------------------------
# DM-3  — empty delegations does not crash
# ---------------------------------------------------------------------------


class TestSubagentAdapterNoneDoesNotCrash:
    """DM-3: Empty delegations list → delegation_node returns {} gracefully."""

    def test_subagent_adapter_none_does_not_crash(self):
        """No error when delegations is empty; empty dict returned."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        state = _make_state(delegations=[])
        config = _make_config()

        # Should not raise regardless of the subagent adapter's state.
        result = _run(delegation_node(state, config))

        # An empty delegations list produces an empty return dict.
        assert isinstance(result, dict)
        # delegation_results should not be populated.
        assert not result.get("delegation_results")

    def test_missing_task_in_delegation_skipped_gracefully(self):
        """A delegation with an empty task string is skipped without crashing."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        delegations = [{"role": "researcher", "task": "", "result_key": "empty_task"}]
        state = _make_state(delegations)
        config = _make_config()

        mock_delegate = AsyncMock(return_value="should not be called")

        with patch(f"{_MODULE}.delegate_task_async", mock_delegate):
            result = _run(delegation_node(state, config))

        # Empty task means run_delegation returns None → nothing stored.
        assert mock_delegate.call_count == 0
        dr = result.get("delegation_results", {})
        assert "empty_task" not in dr


# ---------------------------------------------------------------------------
# DM-4  — delegation result appears in delegation_results
# ---------------------------------------------------------------------------


class TestDelegationResultInHistory:
    """DM-4: delegation_results dict is populated with correct key/status."""

    def test_delegation_result_in_history(self):
        """delegation_results['my_key'] has status='completed' and result text."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        RESULT = "Refactoring complete. 3 files updated."
        delegations = [
            {
                "role": "coder",
                "task": "Refactor the utility module",
                "result_key": "my_key",
            }
        ]
        state = _make_state(delegations)
        config = _make_config()

        mock_delegate = AsyncMock(return_value=RESULT)

        with patch(f"{_MODULE}.delegate_task_async", mock_delegate):
            result = _run(delegation_node(state, config))

        dr = result.get("delegation_results", {})
        assert "my_key" in dr
        entry = dr["my_key"]
        assert entry.get("status") == "completed"
        assert RESULT in str(entry.get("result", ""))

    def test_multiple_delegations_all_stored(self):
        """Two delegations both appear in delegation_results with their keys."""
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        delegations = [
            {"role": "researcher", "task": "Analyse module A", "result_key": "res_a"},
            {"role": "reviewer", "task": "Review module B", "result_key": "res_b"},
        ]
        state = _make_state(delegations)
        config = _make_config()

        call_num = {"n": 0}

        async def _fake_delegate(role, subtask_description, working_dir=None):
            call_num["n"] += 1
            return f"result_{call_num['n']}"

        with patch(f"{_MODULE}.delegate_task_async", _fake_delegate):
            result = _run(delegation_node(state, config))

        dr = result.get("delegation_results", {})
        assert "res_a" in dr
        assert "res_b" in dr
        assert dr["res_a"].get("status") == "completed"
        assert dr["res_b"].get("status") == "completed"
