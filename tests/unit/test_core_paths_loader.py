"""Unit tests for the TUI core paths loader fallback behaviour."""

from __future__ import annotations

import os
from pathlib import Path


def test_get_sessions_dir_fallback(monkeypatch):
    import tui.src.ui._core_paths_loader as loader

    # Simulate inability to load the real core.paths module
    monkeypatch.setattr(loader, "load_core_paths_module", lambda: None)

    d = loader.get_sessions_dir()
    if os.name == "nt":
        expected = Path.home() / "AppData" / "Local" / "CodingAgent" / "sessions"
    else:
        expected = Path.home() / ".coding_agent" / "sessions"
    assert d == expected


def test_get_data_and_config_and_log_dir_fallback(monkeypatch):
    import tui.src.ui._core_paths_loader as loader

    monkeypatch.setattr(loader, "load_core_paths_module", lambda: None)

    data = loader.get_data_dir()
    if os.name == "nt":
        expected_data = Path.home() / "AppData" / "Local" / "CodingAgent"
        expected_log = expected_data / "logs"
        expected_config = expected_data
    else:
        expected_data = Path.home() / ".coding_agent"
        expected_log = Path.home() / ".agent_tui" / "logs"
        expected_config = Path.home() / ".agent_tui"

    assert data == expected_data
    assert loader.get_log_dir() == expected_log
    assert loader.get_config_dir() == expected_config


def test_loader_uses_core_when_available():
    """When src.core.paths is importable, the loader should prefer its helpers."""
    import importlib

    loader = importlib.import_module("tui.src.ui._core_paths_loader")
    # Basic sanity: functions should return Path objects and match src.core.paths
    import src.core.paths as core_paths

    assert loader.get_data_dir() == core_paths.get_data_dir()
    assert loader.get_sessions_dir() == core_paths.get_sessions_dir()
    assert loader.get_config_dir() == core_paths.get_config_dir()
