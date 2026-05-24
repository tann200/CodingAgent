"""Tests for StatusBarMixin (P1-6 Phase A).

These tests exercise the mixin in isolation using a lightweight stub class that
simulates the Textual widget API without requiring a full Textual app.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Stub infrastructure
# ---------------------------------------------------------------------------


class _FakeWidget:
    """Minimal substitute for a Textual Static widget."""

    def __init__(self) -> None:
        self._content: str = ""
        self._classes: set[str] = set()

    def update(self, content: str) -> None:
        self._content = content

    def add_class(self, *names: str) -> None:
        self._classes.update(names)

    def remove_class(self, *names: str) -> None:
        self._classes -= set(names)


class _StubApp:
    """Minimal host class that satisfies StatusBarMixin's attribute contract."""

    def __init__(self) -> None:
        self.active_role: str = "lead_architect"
        self.is_streaming: bool = False
        self.agent_running: bool = False
        self.total_tokens: int = 0
        self.context_window: int = 8192
        self._pending_perm_count: int = 0
        self.sub_title: str = ""
        self._widgets: dict[str, _FakeWidget] = {}

    def _make(self, widget_id: str) -> _FakeWidget:
        w = _FakeWidget()
        self._widgets[widget_id] = w
        return w

    def query_one(self, selector: str, widget_type: type) -> _FakeWidget:
        widget_id = selector.lstrip("#")
        if widget_id not in self._widgets:
            raise Exception(f"widget {selector!r} not found")
        return self._widgets[widget_id]


# Compose stub + mixin
from tui.tui_src.ui.components.status_bar import StatusBarMixin, ROLE_LABELS, ROLE_COLORS


class _App(_StubApp, StatusBarMixin):
    pass


# ---------------------------------------------------------------------------
# ROLE_LABELS / ROLE_COLORS exports
# ---------------------------------------------------------------------------


class TestRoleDicts:
    def test_role_labels_keys(self):
        assert set(ROLE_LABELS) == {
            "lead_architect",
            "full_stack_engineer",
            "qa_lead",
            "system",
        }

    def test_role_colors_keys_match_labels(self):
        assert set(ROLE_COLORS) == set(ROLE_LABELS)

    def test_role_colors_are_hex(self):
        for role, color in ROLE_COLORS.items():
            assert color.startswith("#"), f"{role!r} color {color!r} not hex"
            assert len(color) == 7, f"{role!r} color {color!r} wrong length"


# ---------------------------------------------------------------------------
# _update_perm_badge
# ---------------------------------------------------------------------------


class TestUpdatePermBadge:
    def setup_method(self):
        self.app = _App()
        self.app._make("perm_count_chip")

    def test_initial_zero_clears_chip(self):
        self.app._update_perm_badge(0)
        assert self.app._widgets["perm_count_chip"]._content == ""

    def test_positive_delta_shows_count(self):
        self.app._update_perm_badge(3)
        assert "3" in self.app._widgets["perm_count_chip"]._content
        assert "Permission" in self.app._widgets["perm_count_chip"]._content

    def test_multiple_deltas_accumulate(self):
        self.app._update_perm_badge(2)
        self.app._update_perm_badge(1)
        assert self.app._pending_perm_count == 3

    def test_negative_delta_decrements(self):
        self.app._pending_perm_count = 5
        self.app._update_perm_badge(-2)
        assert self.app._pending_perm_count == 3

    def test_count_never_goes_negative(self):
        self.app._pending_perm_count = 0
        self.app._update_perm_badge(-10)
        assert self.app._pending_perm_count == 0

    def test_returns_to_zero_clears_chip(self):
        self.app._pending_perm_count = 1
        self.app._update_perm_badge(-1)
        assert self.app._widgets["perm_count_chip"]._content == ""

    def test_missing_widget_does_not_raise(self):
        app = _App()  # no widget registered
        app._update_perm_badge(1)  # must not raise


# ---------------------------------------------------------------------------
# _update_role_display
# ---------------------------------------------------------------------------


class TestUpdateRoleDisplay:
    def setup_method(self):
        self.app = _App()
        self.app._make("sb_role")

    def test_known_role_uses_label(self):
        self.app._update_role_display("lead_architect")
        assert "LEAD ARCHITECT" in self.app._widgets["sb_role"]._content

    def test_known_role_uses_color(self):
        self.app._update_role_display("qa_lead")
        assert ROLE_COLORS["qa_lead"] in self.app._widgets["sb_role"]._content

    def test_unknown_role_uppercases(self):
        self.app._update_role_display("custom_role")
        assert "CUSTOM ROLE" in self.app._widgets["sb_role"]._content

    def test_sets_sub_title(self):
        self.app._update_role_display("full_stack_engineer")
        assert "FULL STACK ENGINEER" in self.app.sub_title

    def test_missing_widget_does_not_raise(self):
        app = _App()
        app._update_role_display("lead_architect")  # must not raise


# ---------------------------------------------------------------------------
# _update_status_bar
# ---------------------------------------------------------------------------


class TestUpdateStatusBar:
    def setup_method(self):
        self.app = _App()
        self.app._make("status_left")
        self.app.active_role = "lead_architect"
        self.app.total_tokens = 1000
        self.app.context_window = 8192

    def test_shows_role_label(self):
        self.app._update_status_bar()
        assert "LEAD ARCHITECT" in self.app._widgets["status_left"]._content

    def test_shows_token_counts(self):
        self.app._update_status_bar()
        assert "1,000" in self.app._widgets["status_left"]._content
        assert "8,192" in self.app._widgets["status_left"]._content

    def test_no_streaming_flag_when_not_streaming(self):
        self.app.is_streaming = False
        self.app._update_status_bar()
        assert "streaming" not in self.app._widgets["status_left"]._content

    def test_streaming_flag_shown(self):
        self.app.is_streaming = True
        self.app._update_status_bar()
        assert "streaming" in self.app._widgets["status_left"]._content

    def test_running_flag_shown(self):
        self.app.agent_running = True
        self.app._update_status_bar()
        assert "running" in self.app._widgets["status_left"]._content

    def test_missing_widget_does_not_raise(self):
        app = _App()
        app._update_status_bar()  # must not raise


# ---------------------------------------------------------------------------
# _update_mcp_status_chip
# ---------------------------------------------------------------------------


class TestUpdateMcpStatusChip:
    def setup_method(self):
        self.app = _App()
        self.app._make("mcp_status_chip")

    def _content(self) -> str:
        return self.app._widgets["mcp_status_chip"]._content

    def test_not_running_shows_dim(self):
        self.app._update_mcp_status_chip(running=False, count=0, has_error=False)
        assert "dim" in self._content()

    def test_running_no_error_shows_green(self):
        self.app._update_mcp_status_chip(running=True, count=3, has_error=False)
        assert "green" in self._content()
        assert "3" in self._content()

    def test_running_with_error_shows_red(self):
        self.app._update_mcp_status_chip(running=True, count=2, has_error=True)
        assert "#ff5555" in self._content()
        assert "2" in self._content()

    def test_running_no_count_no_error(self):
        self.app._update_mcp_status_chip(running=True, count=0, has_error=False)
        assert "green" in self._content()
        assert "MCP" in self._content()

    def test_running_no_count_with_error(self):
        self.app._update_mcp_status_chip(running=True, count=0, has_error=True)
        assert "#ff5555" in self._content()

    def test_missing_widget_does_not_raise(self):
        app = _App()
        app._update_mcp_status_chip(running=True, count=1, has_error=False)


# ---------------------------------------------------------------------------
# _update_provider_status_widgets
# ---------------------------------------------------------------------------


class TestUpdateProviderStatusWidgets:
    def setup_method(self):
        self.app = _App()
        self.app._make("sb_provider")
        self.app._make("sb_model_info")
        self.app._make("provider_banner")

    def test_provider_name_in_sb_provider(self):
        self.app._update_provider_status_widgets("openai", "connected")
        assert "openai" in self.app._widgets["sb_provider"]._content

    def test_status_label_connected(self):
        self.app._update_provider_status_widgets("openai", "connected")
        assert "connected" in self.app._widgets["sb_provider"]._content

    def test_status_label_failed_mapped_to_error(self):
        self.app._update_provider_status_widgets("openai", "failed")
        assert "error" in self.app._widgets["sb_provider"]._content

    def test_status_label_disconnected(self):
        self.app._update_provider_status_widgets("openai", "disconnected")
        assert "not connected" in self.app._widgets["sb_provider"]._content

    def test_status_label_initializing(self):
        self.app._update_provider_status_widgets("openai", "initializing")
        assert "initializing" in self.app._widgets["sb_provider"]._content

    def test_model_updates_sb_model_info(self):
        self.app._update_provider_status_widgets("openai", "connected", model="gpt-4o")
        assert "gpt-4o" in self.app._widgets["sb_model_info"]._content

    def test_model_updates_provider_banner(self):
        self.app._update_provider_status_widgets("openai", "connected", model="gpt-4o")
        banner = self.app._widgets["provider_banner"]._content
        assert "openai" in banner
        assert "gpt-4o" in banner

    def test_no_model_skips_model_widgets(self):
        self.app._update_provider_status_widgets("openai", "connected")
        assert self.app._widgets["sb_model_info"]._content == ""

    def test_missing_widgets_do_not_raise(self):
        app = _App()
        app._update_provider_status_widgets("openai", "connected", model="gpt-4o")


# ---------------------------------------------------------------------------
# _update_status_text
# ---------------------------------------------------------------------------


class TestUpdateStatusText:
    def setup_method(self):
        self.app = _App()
        self.app._make("sb_status")

    def test_message_included(self):
        self.app._update_status_text("idle")
        assert "idle" in self.app._widgets["sb_status"]._content

    def test_prefix_present(self):
        self.app._update_status_text("processing")
        assert "Status:" in self.app._widgets["sb_status"]._content

    def test_missing_widget_does_not_raise(self):
        app = _App()
        app._update_status_text("idle")
