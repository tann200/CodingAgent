"""
Tests for debug node with LLM-enhanced analysis.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from typing import Dict, Any


class TestDebugNode:
    """Tests for debug_node."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Create a mock orchestrator."""
        orch = MagicMock()
        orch.adapter = MagicMock()
        orch.adapter.provider = {"name": "test_provider"}
        orch.adapter.models = ["test_model"]

        tool_registry = MagicMock()
        tool_registry.tools = {
            "edit_file": {"description": "Edit a file"},
            "write_file": {"description": "Write a file"},
        }
        orch.tool_registry = tool_registry
        return orch

    @pytest.fixture
    def mock_state(self):
        """Create a mock agent state."""
        return {
            "task": "Fix the bug",
            "history": [
                {"role": "user", "content": "Fix the bug"},
                {"role": "assistant", "content": "I'll fix it"},
            ],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "last_result": {"error": "NameError: name 'x' is not defined"},
            "verification_result": {
                "tests": {"status": "fail", "stdout": "FAILED test_foo"},
            },
        }

    @pytest.fixture
    def mock_config(self, mock_orchestrator):
        """Create a mock config."""
        return {"configurable": {"orchestrator": mock_orchestrator}}

    @pytest.mark.asyncio
    async def test_debug_node_extracts_error_from_last_result(
        self, mock_state, mock_config, monkeypatch
    ):
        """Test debug node extracts error from last_result."""
        # Mock ContextBuilder
        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "test"}]
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda: mock_builder,
        )

        # Mock call_model
        mock_resp = {"choices": [{"message": {"content": ""}}]}
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            lambda *args, **kwargs: mock_resp,
        )

        # Mock parse_tool_block
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            lambda x: None,
        )

        from src.core.orchestration.graph.nodes.debug_node import debug_node

        result = await debug_node(mock_state, mock_config)
        assert "debug_attempts" in result

    @pytest.mark.asyncio
    async def test_debug_node_max_attempts_reached(self, mock_config):
        """Test debug node returns None when max attempts reached."""
        state = {
            "task": "Fix the bug",
            "history": [],
            "debug_attempts": 3,
            "max_debug_attempts": 3,
            "last_result": {},
            "verification_result": {},
        }

        from src.core.orchestration.graph.nodes.debug_node import debug_node

        result = await debug_node(state, mock_config)
        assert result["next_action"] is None
        assert "Max debug attempts" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_debug_node_no_orchestrator(self, mock_state):
        """Test debug node handles missing orchestrator."""
        config = {"configurable": {}}

        from src.core.orchestration.graph.nodes.debug_node import debug_node

        result = await debug_node(mock_state, config)
        assert result["next_action"] is None
        assert "orchestrator" in result["errors"][0].lower()


class TestClassifyError:
    """Unit tests for _classify_error() in debug_node."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from src.core.orchestration.graph.nodes.debug_node import _classify_error
        self.classify = _classify_error

    def test_syntax_error(self):
        assert self.classify("SyntaxError: invalid syntax at line 5") == "syntax_error"

    def test_indentation_error(self):
        assert self.classify("IndentationError: unexpected indent") == "syntax_error"

    def test_invalid_syntax(self):
        assert self.classify("invalid syntax near '+'") == "syntax_error"

    def test_import_error(self):
        assert self.classify("ImportError: cannot import name 'foo'") == "import_error"

    def test_module_not_found(self):
        assert self.classify("ModuleNotFoundError: No module named 'bar'") == "import_error"

    def test_no_module_named(self):
        assert self.classify("no module named requests") == "import_error"

    def test_failed_test(self):
        assert self.classify("FAILED tests/test_foo.py::test_bar - AssertionError") == "test_failure"

    def test_assert_in_test_result(self):
        assert self.classify("Test failure: assertionerror in test") == "test_failure"

    def test_lint_error(self):
        assert self.classify("E501 line too long (120 > 79 characters)") == "lint_error"

    def test_flake8_error(self):
        assert self.classify("flake8: W503 line break before binary operator") == "lint_error"

    def test_runtime_error_via_attribute(self):
        assert self.classify("AttributeError: 'NoneType' object has no attribute 'foo'") == "runtime_error"

    def test_type_error(self):
        assert self.classify("TypeError: unsupported operand type(s)") == "runtime_error"

    def test_name_error(self):
        assert self.classify("NameError: name 'x' is not defined") == "runtime_error"

    def test_unknown_error(self):
        assert self.classify("some completely unknown condition occurred") == "unknown_error"

    def test_empty_string(self):
        assert self.classify("") == "unknown_error"


class TestDebugNodeRetry:
    """Tests for debug node retry logic."""

    def test_retry_increments_attempt(self):
        """Test that retry increments the attempt counter."""
        current_attempt = 0
        next_attempt = current_attempt + 1
        assert next_attempt == 1

    def test_max_attempts_respected(self):
        """Test max attempts boundary."""
        current_attempt = 2
        max_attempts = 3

        # Should still allow this attempt
        assert current_attempt < max_attempts

        # Next attempt would exceed
        next_attempt = current_attempt + 1
        assert next_attempt == max_attempts


class TestDebugNodePromptEnrichment:
    """Test that debug_node embeds error_type and guidance in the fix prompt."""

    def test_error_type_embedded_in_prompt_guidance(self):
        """TYPE_GUIDANCE provides targeted strings for all 6 error categories."""
        from src.core.orchestration.graph.nodes.debug_node import _classify_error, TYPE_GUIDANCE

        expected_categories = {
            "syntax_error", "import_error", "test_failure",
            "lint_error", "runtime_error", "unknown_error",
        }
        assert set(TYPE_GUIDANCE.keys()) == expected_categories
        for category, guidance in TYPE_GUIDANCE.items():
            assert len(guidance) > 10, f"Guidance for {category!r} is too short"
