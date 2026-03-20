"""
End-to-End tests for CodingAgent.

These tests run full agent workflows and require:
- Python 3.11+
- Ollama or LM Studio running (for full E2E tests)
- Environment variable CI=true to run in CI (skips slow tests)

Run with:
    pytest tests/e2e/ -v
    pytest tests/e2e/ -v -m "not slow"  # Skip slow tests
"""

import pytest
import os
import asyncio
from pathlib import Path

# Markers
pytestmark = [
    pytest.mark.e2e,
]


def pytest_configure(config):
    """Configure E2E test markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "ollama: marks tests requiring Ollama")
    config.addinivalue_line("markers", "lmstudio: marks tests requiring LM Studio")
    config.addinivalue_line("markers", "scenario: marks scenario benchmark tests")


def pytest_collection_modifyitems(config, items):
    """Skip slow tests unless explicitly requested or CI=true."""
    if os.environ.get("CI") == "true":
        return  # Run all tests in CI

    skip_slow = pytest.mark.skip(reason="Slow test - run with CI=true or -m 'slow'")
    for item in items:
        if "slow" in item.keywords and not (
            config.getoption("-m") and "slow" in config.getoption("-m")
        ):
            item.add_marker(skip_slow)


class TestBasicE2EWorkflows:
    """Basic end-to-end workflow tests."""

    @pytest.mark.slow
    @pytest.mark.ollama
    async def test_simple_file_read(self):
        """Test simple file read workflow."""
        pytest.skip("Requires Ollama running - run with CI=true to execute")

    @pytest.mark.slow
    async def test_simple_task_with_mock(self):
        """Test simple task with mocked LLM."""
        pytest.skip("Test implementation pending")


class TestAgentBehaviorE2E:
    """End-to-end agent behavior tests."""

    @pytest.mark.slow
    async def test_agent_read_before_edit(self):
        """Test agent follows read-before-edit pattern."""
        pytest.skip("Test implementation pending")

    @pytest.mark.slow
    async def test_agent_loop_prevention(self):
        """Test agent doesn't loop indefinitely."""
        pytest.skip("Test implementation pending")


class TestScenarioBenchmarks:
    """SWE-bench style scenario benchmarks."""

    @pytest.mark.slow
    @pytest.mark.scenario
    async def test_bug_fix_scenario(self):
        """Test bug fix scenario."""
        pytest.skip("Scenario benchmarks pending implementation")

    @pytest.mark.slow
    @pytest.mark.scenario
    async def test_feature_add_scenario(self):
        """Test feature addition scenario."""
        pytest.skip("Scenario benchmarks pending implementation")
