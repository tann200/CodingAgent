"""
Tests for analyst_delegation_node (#56 — early delegation before planning).
"""
import pytest
from unittest.mock import patch, AsyncMock

from src.core.orchestration.graph.nodes.analyst_delegation_node import (
    analyst_delegation_node,
)
from src.core.orchestration.graph.builder import should_after_analysis


# ---------------------------------------------------------------------------
# analyst_delegation_node unit tests
# ---------------------------------------------------------------------------

class TestAnalystDelegationNode:

    @pytest.fixture
    def config(self):
        return {"configurable": {"orchestrator": None}}

    @pytest.fixture
    def base_state(self, tmp_path):
        return {
            "task": "refactor the authentication module",
            "analysis_summary": "Found 5 relevant files",
            "relevant_files": ["src/auth.py", "src/models.py"],
            "working_dir": str(tmp_path),
        }

    @pytest.mark.asyncio
    async def test_returns_analyst_findings_key(self, base_state, config):
        """Node must return a dict with analyst_findings key."""
        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value="<findings>key classes: AuthManager</findings>",
        ):
            result = await analyst_delegation_node(base_state, config)
        assert "analyst_findings" in result

    @pytest.mark.asyncio
    async def test_findings_contain_subagent_output(self, base_state, config):
        """analyst_findings should be the string returned by the analyst subagent."""
        findings_text = "<findings>Risk: shared session state</findings>"
        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value=findings_text,
        ):
            result = await analyst_delegation_node(base_state, config)
        assert result["analyst_findings"] == findings_text

    @pytest.mark.asyncio
    async def test_subagent_called_with_analyst_role(self, base_state, config):
        """Subagent must be invoked with role='analyst'."""
        captured = {}

        async def mock_delegate(role, subtask_description, working_dir):
            captured["role"] = role
            return "findings"

        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            await analyst_delegation_node(base_state, config)
        assert captured["role"] == "analyst"

    @pytest.mark.asyncio
    async def test_subtask_includes_task_and_files(self, base_state, config):
        """Subtask description should reference the task and relevant_files."""
        captured = {}

        async def mock_delegate(role, subtask_description, working_dir):
            captured["subtask"] = subtask_description
            return "ok"

        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            await analyst_delegation_node(base_state, config)

        assert "refactor the authentication module" in captured["subtask"]
        assert "src/auth.py" in captured["subtask"]

    @pytest.mark.asyncio
    async def test_error_returns_empty_findings(self, base_state, config):
        """If the subagent raises, analyst_findings must be empty string (not propagate)."""
        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=RuntimeError("subagent unavailable"),
        ):
            result = await analyst_delegation_node(base_state, config)
        assert result["analyst_findings"] == ""

    @pytest.mark.asyncio
    async def test_non_string_return_coerced_to_str(self, base_state, config):
        """Non-string subagent results should be coerced to string."""
        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value={"findings": "some dict"},
        ):
            result = await analyst_delegation_node(base_state, config)
        assert isinstance(result["analyst_findings"], str)

    @pytest.mark.asyncio
    async def test_working_dir_passed_to_subagent(self, base_state, config, tmp_path):
        """working_dir must be forwarded to the subagent."""
        captured = {}

        async def mock_delegate(role, subtask_description, working_dir):
            captured["working_dir"] = working_dir
            return "ok"

        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            await analyst_delegation_node(base_state, config)

        assert captured["working_dir"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_empty_relevant_files_still_works(self, config, tmp_path):
        """Node should not crash if relevant_files is empty."""
        state = {
            "task": "refactor auth",
            "analysis_summary": "no files found",
            "relevant_files": [],
            "working_dir": str(tmp_path),
        }
        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value="findings",
        ):
            result = await analyst_delegation_node(state, config)
        assert "analyst_findings" in result


# ---------------------------------------------------------------------------
# should_after_analysis routing tests
# ---------------------------------------------------------------------------

class TestShouldAfterAnalysis:
    """#56: should_after_analysis routes complex → analyst_delegation, simple → planning."""

    def _state(self, task="", relevant_files=None, current_plan=None):
        return {
            "task": task,
            "relevant_files": relevant_files or [],
            "current_plan": current_plan or [],
        }

    def test_simple_task_goes_to_planning(self):
        state = self._state(task="read a file")
        assert should_after_analysis(state) == "planning"

    def test_complex_keyword_routes_to_analyst_delegation(self):
        state = self._state(task="refactor the entire authentication module")
        assert should_after_analysis(state) == "analyst_delegation"

    def test_many_relevant_files_routes_to_analyst_delegation(self):
        # >3 relevant files → complex
        state = self._state(
            task="update config",
            relevant_files=["a.py", "b.py", "c.py", "d.py"],
        )
        assert should_after_analysis(state) == "analyst_delegation"

    def test_existing_plan_routes_to_analyst_delegation(self):
        # >=2 plan steps already set → complex
        state = self._state(
            task="update config",
            current_plan=[{"description": "step1"}, {"description": "step2"}],
        )
        assert should_after_analysis(state) == "analyst_delegation"

    def test_few_files_not_complex(self):
        state = self._state(task="fix typo", relevant_files=["a.py", "b.py"])
        assert should_after_analysis(state) == "planning"
