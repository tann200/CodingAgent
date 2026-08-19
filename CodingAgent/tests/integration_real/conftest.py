"""
Pytest configuration for integration_real tests.

This file configures markers and fixtures specific to real integration tests.
"""

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration_real: Real integration tests that integrate multiple components with minimal mocking"
    )
