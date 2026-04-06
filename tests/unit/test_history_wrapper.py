"""Tests for HistoryWrapper utility (migrated from src.ui — LEGACY-02 / LEGACY-04).

Previously tested ``src.ui.textual_app_impl._HistoryWrapper``; now tests the
absorbed ``src.core.utils.HistoryWrapper`` directly.
"""

import pytest

from src.core.utils import HistoryWrapper


def test_history_wrapper_basic():
    """HistoryWrapper exposes checkpoint() and behaves like a list."""
    hw = HistoryWrapper(["one", "two"])
    # checkpoint should be callable and not raise
    assert callable(hw.checkpoint)
    hw.checkpoint()
    # list-like behavior
    assert len(hw) == 2
    assert hw[0] == "one"
    assert list(iter(hw)) == ["one", "two"]
    hw.append("three")
    assert hw.to_list()[-1] == "three"


def test_history_wrapper_empty_init():
    hw = HistoryWrapper()
    assert len(hw) == 0
    hw.append("hello")
    assert len(hw) == 1


def test_history_wrapper_extend():
    hw = HistoryWrapper(["a"])
    hw.extend(["b", "c"])
    assert hw.to_list() == ["a", "b", "c"]


def test_history_wrapper_clear():
    hw = HistoryWrapper(["x", "y"])
    hw.clear()
    assert len(hw) == 0


def test_history_wrapper_repr():
    hw = HistoryWrapper(["hello"])
    assert "hello" in repr(hw)
