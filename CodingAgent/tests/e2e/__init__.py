"""
End-to-End tests for CodingAgent.

These tests run full agent workflows. All tests use a mocked LLM backend
so they run in CI without a live local provider.

Run with:
    pytest tests/e2e/ -v
    pytest tests/e2e/ -v -m "not slow"  # Skip slow tests requiring a live provider
"""

import os
import pytest

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
