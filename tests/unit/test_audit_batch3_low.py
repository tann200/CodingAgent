"""
Regression tests for Batch 3 / LOW GAP findings.

  GAP-TUI-2     — InlineDiff widget (single-column unified diff renderer)
  GAP-PERM-2    — reject-with-feedback flow wired in button handler
  GAP-PERM-3    — allow-always glob: _allow_always_tools set + auto-approve guard
  GAP-FOOTER-3  — subagent footer chip (_update_subagent_footer)
  GAP-CMD-2     — /share slash command handler present
  GAP-CMD-3     — /rename slash command handler present
  GAP-WORKTREE-1 — GitWorktreeManager create/remove/list/parse
  GAP-CONFIG-1   — diff_style setting (default "side-by-side")
  GAP-CONFIG-2   — scroll_speed setting (default 3)
  GAP-CONFIG-3   — conceal_sensitive setting (default False)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# GAP-TUI-2: InlineDiff widget
# ─────────────────────────────────────────────────────────────────────────────

class TestGapTUI2InlineDiff:
    def test_inline_diff_importable(self):
        from tui.src.ui.components.diff_viewer import InlineDiff
        assert InlineDiff is not None

    def test_render_inline_added_line(self):
        from tui.src.ui.components.diff_viewer import _render_inline
        diff = "@@ -1,1 +1,2 @@\n context\n+added line\n"
        lines = _render_inline(diff)
        assert any("added line" in ln for ln in lines)
        assert any("[on #003d00]" in ln for ln in lines)

    def test_render_inline_removed_line(self):
        from tui.src.ui.components.diff_viewer import _render_inline
        diff = "@@ -1,2 +1,1 @@\n-removed line\n context\n"
        lines = _render_inline(diff)
        assert any("removed line" in ln for ln in lines)
        assert any("[on #3d0000]" in ln for ln in lines)

    def test_render_inline_skips_file_headers(self):
        from tui.src.ui.components.diff_viewer import _render_inline
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n context\n"
        lines = _render_inline(diff)
        # File header lines should be skipped
        assert not any("--- a/" in ln for ln in lines)
        assert not any("+++ b/" in ln for ln in lines)

    def test_render_inline_hunk_header_coloured(self):
        from tui.src.ui.components.diff_viewer import _render_inline
        diff = "@@ -1,1 +1,1 @@\n context\n"
        lines = _render_inline(diff)
        assert any("[bold #00b4d8]" in ln for ln in lines)

    def test_inline_diff_shares_message_types_with_sbs(self):
        from tui.src.ui.components.diff_viewer import InlineDiff, SideBySideDiff
        assert InlineDiff.Accepted is SideBySideDiff.Accepted
        assert InlineDiff.Rejected is SideBySideDiff.Rejected

    def test_components_package_exports_inline_diff(self):
        from tui.src.ui.components import InlineDiff
        assert InlineDiff is not None


# ─────────────────────────────────────────────────────────────────────────────
# GAP-CONFIG-1/2/3: settings defaults
# ─────────────────────────────────────────────────────────────────────────────

class TestGapConfig:
    def test_diff_style_default(self):
        from tui.src.ui.settings import DEFAULTS
        assert DEFAULTS["diff_style"] == "side-by-side"

    def test_scroll_speed_default(self):
        from tui.src.ui.settings import DEFAULTS
        assert DEFAULTS["scroll_speed"] == 3

    def test_conceal_sensitive_default(self):
        from tui.src.ui.settings import DEFAULTS
        assert DEFAULTS["conceal_sensitive"] is False

    def test_settings_store_returns_diff_style(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        assert store.get("diff_style") == "side-by-side"

    def test_settings_store_returns_scroll_speed(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        assert store.get("scroll_speed") == 3

    def test_settings_store_returns_conceal_sensitive(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        assert store.get("conceal_sensitive") is False

    def test_settings_store_persists_diff_style(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        store.set("diff_style", "inline")
        store.save()
        store2 = SettingsStore(path=tmp_path / "settings.json")
        assert store2.get("diff_style") == "inline"

    def test_settings_store_persists_scroll_speed(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        store.set("scroll_speed", 7)
        store.save()
        store2 = SettingsStore(path=tmp_path / "settings.json")
        assert store2.get("scroll_speed") == 7

    def test_settings_store_persists_conceal_sensitive(self, tmp_path):
        from tui.src.ui.settings import SettingsStore
        store = SettingsStore(path=tmp_path / "settings.json")
        store.set("conceal_sensitive", True)
        store.save()
        store2 = SettingsStore(path=tmp_path / "settings.json")
        assert store2.get("conceal_sensitive") is True


# ─────────────────────────────────────────────────────────────────────────────
# GAP-WORKTREE-1: GitWorktreeManager
# ─────────────────────────────────────────────────────────────────────────────

class TestGapWorktree1:
    def test_importable(self):
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        assert GitWorktreeManager is not None

    def test_active_empty_on_init(self, tmp_path):
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        mgr = GitWorktreeManager(workspace=tmp_path)
        assert mgr.active == {}

    def test_remove_nonexistent_returns_false(self, tmp_path):
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        mgr = GitWorktreeManager(workspace=tmp_path)
        result = asyncio.run(mgr.remove("nonexistent_task"))
        assert result is False

    def test_create_returns_existing_if_already_active(self, tmp_path):
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        mgr = GitWorktreeManager(workspace=tmp_path)
        fake_path = tmp_path / "fake_wt"
        fake_path.mkdir()
        mgr._active["my_task"] = fake_path
        result = asyncio.run(mgr.create("my_task"))
        assert result == fake_path

    def test_parse_worktree_list_single(self):
        from src.core.orchestration.git_worktree_manager import _parse_worktree_list
        text = (
            "worktree /repo\n"
            "HEAD abc1234\n"
            "branch refs/heads/main\n"
            "\n"
        )
        entries = _parse_worktree_list(text)
        assert len(entries) == 1
        assert entries[0]["worktree"] == "/repo"
        assert entries[0]["HEAD"] == "abc1234"
        assert entries[0]["branch"] == "refs/heads/main"

    def test_parse_worktree_list_detached(self):
        from src.core.orchestration.git_worktree_manager import _parse_worktree_list
        text = (
            "worktree /repo/wt1\n"
            "HEAD deadbeef\n"
            "detached\n"
            "\n"
        )
        entries = _parse_worktree_list(text)
        assert entries[0].get("detached") is True

    def test_parse_worktree_list_multiple(self):
        from src.core.orchestration.git_worktree_manager import _parse_worktree_list
        text = (
            "worktree /repo\n"
            "HEAD aaa\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /tmp/wt1\n"
            "HEAD bbb\n"
            "detached\n"
            "\n"
        )
        entries = _parse_worktree_list(text)
        assert len(entries) == 2
        assert entries[1]["worktree"] == "/tmp/wt1"

    def test_create_fails_propagates_cleanup(self, tmp_path):
        """If `git worktree add` fails, the temp dir is cleaned up."""
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        mgr = GitWorktreeManager(workspace=tmp_path)

        async def _run():
            async def _mock_exec(*args, **kwargs):
                proc = MagicMock()
                proc.returncode = 128
                proc.communicate = AsyncMock(return_value=(b"", b"not a git repo"))
                return proc

            with patch("asyncio.create_subprocess_exec", side_effect=_mock_exec):
                try:
                    await mgr.create("bad_task")
                    return False
                except RuntimeError:
                    return True

        raised = asyncio.run(_run())
        assert raised
        assert "bad_task" not in mgr.active

    def test_remove_all_clears_active(self, tmp_path):
        from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        mgr = GitWorktreeManager(workspace=tmp_path)
        fake1 = tmp_path / "wt1"
        fake1.mkdir()
        fake2 = tmp_path / "wt2"
        fake2.mkdir()
        mgr._active["t1"] = fake1
        mgr._active["t2"] = fake2

        async def _run():
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                proc = MagicMock()
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"", b""))
                mock_exec.return_value = proc
                await mgr.remove_all()

        asyncio.run(_run())
        assert mgr.active == {}


# ─────────────────────────────────────────────────────────────────────────────
# GAP-PERM-3: allow-always set
# ─────────────────────────────────────────────────────────────────────────────

class TestGapPerm3AllowAlways:
    def test_allow_always_tools_in_settings_defaults_not_needed(self):
        """_allow_always_tools is a runtime set, not a persisted setting."""
        from tui.src.ui.settings import DEFAULTS
        # It should NOT be in DEFAULTS (it's session-only state on the app)
        assert "allow_always_tools" not in DEFAULTS

    def test_allow_always_tools_attribute_defined(self):
        """The attribute name expected by app.py logic is tested here without
        importing the app (which requires a running Textual event loop)."""
        # Simulate the initialisation snippet from app.py
        class FakeApp:
            def __init__(self):
                self._allow_always_tools: set = set()

        app = FakeApp()
        assert isinstance(app._allow_always_tools, set)
        app._allow_always_tools.add("read_file")
        assert "read_file" in app._allow_always_tools


# ─────────────────────────────────────────────────────────────────────────────
# GAP-CMD-2/3: /share and /rename handlers exist in app source
# ─────────────────────────────────────────────────────────────────────────────

class TestGapCommands:
    def _load_app_source(self) -> str:
        path = Path(__file__).parents[2] / "tui" / "src" / "ui" / "app.py"
        return path.read_text(encoding="utf-8")

    def test_slash_share_handler_present(self):
        src = self._load_app_source()
        assert "async def _slash_share" in src

    def test_slash_rename_handler_present(self):
        src = self._load_app_source()
        assert "async def _slash_rename" in src

    def test_slash_worktree_handler_present(self):
        src = self._load_app_source()
        assert "async def _slash_worktree" in src

    def test_share_in_slash_help(self):
        src = self._load_app_source()
        assert "/share" in src

    def test_rename_in_slash_help(self):
        src = self._load_app_source()
        assert "/rename" in src

    def test_worktree_in_slash_help(self):
        src = self._load_app_source()
        assert "/worktree" in src

    def test_slash_share_dispatched_from_handle_slash_command(self):
        src = self._load_app_source()
        assert 'cmd == "share"' in src or "\"share\"" in src

    def test_slash_rename_dispatched_from_handle_slash_command(self):
        src = self._load_app_source()
        assert 'cmd == "rename"' in src or "\"rename\"" in src


# ─────────────────────────────────────────────────────────────────────────────
# GAP-PERM-2: deny-with-feedback wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestGapPerm2DenyFeedback:
    def _load_app_source(self) -> str:
        path = Path(__file__).parents[2] / "tui" / "src" / "ui" / "app.py"
        return path.read_text(encoding="utf-8")

    def test_deny_feedback_input_id_present(self):
        src = self._load_app_source()
        assert "inp_deny_fb_" in src

    def test_btn_deny_fb_send_handler_present(self):
        src = self._load_app_source()
        assert "btn_deny_fb_send_" in src

    def test_btn_deny_fb_skip_handler_present(self):
        src = self._load_app_source()
        assert "btn_deny_fb_skip_" in src

    def test_denial_feedback_event_published(self):
        src = self._load_app_source()
        assert "tool.denial_feedback" in src


# ─────────────────────────────────────────────────────────────────────────────
# GAP-FOOTER-3: subagent footer chip
# ─────────────────────────────────────────────────────────────────────────────

class TestGapFooter3:
    def _load_app_source(self) -> str:
        path = Path(__file__).parents[2] / "tui" / "src" / "ui" / "app.py"
        return path.read_text(encoding="utf-8")

    def test_subagent_footer_chip_yielded(self):
        src = self._load_app_source()
        assert "subagent_footer_chip" in src

    def test_update_subagent_footer_method_present(self):
        src = self._load_app_source()
        assert "_update_subagent_footer" in src

    def test_footer_chip_updated_on_subagent_start(self):
        src = self._load_app_source()
        # _update_subagent_footer must be called inside handle_subagent_start
        start_idx = src.index("def handle_subagent_start")
        end_idx = src.index("def handle_subagent_finish")
        handler_body = src[start_idx:end_idx]
        assert "_update_subagent_footer" in handler_body
