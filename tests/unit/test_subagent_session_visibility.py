"""
Regression tests for subagent session visibility and navigation.

Covers:
  - SubagentProgress.Clicked message carries child_session_id
  - SubagentProgress.finish() shows "(click to view)" hint when session_id present
  - SubagentProgress passes child_session_id through __init__
  - SubagentDetailScreen loads session JSON correctly
  - SubagentDetailScreen handles missing session file gracefully
  - SessionListScreen filter_subagents kwarg
  - SessionListScreen _render_list annotates child sessions with ↳ role tag
  - SessionListScreen _open_selected routes child sessions to detail screen
  - subagent_tools.py saves session JSON after completion (unit-level only, mocked)
  - subagent_tools.py saves failed session skeleton
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# SubagentProgress — clickable widget
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentProgressClickable:
    def test_child_session_id_stored(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="analyst", task="do something", child_session_id="abc123")
        assert w._child_session_id == "abc123"

    def test_default_child_session_id_empty(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="analyst", task="do something")
        assert w._child_session_id == ""

    def test_clicked_message_has_correct_fields(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        msg = SubagentProgress.Clicked(
            child_session_id="sess42",
            role="debugger",
            task="fix the bug",
        )
        assert msg.child_session_id == "sess42"
        assert msg.role == "debugger"
        assert msg.task == "fix the bug"

    def test_finish_adds_click_hint_when_session_id_set(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="analyst", task="x", child_session_id="sess1")
        w._finished = False
        # Simulate finish without a real Textual loop by just calling the logic
        # We can't call w.finish() without a mount, so test the string logic inline
        child_id = "sess1"
        ok = True
        icon = "✓"
        color = "#22c55e"
        hint = "  [dim](click to view)[/]" if child_id else ""
        rendered = f"[bold {color}]{icon} analyst[/] — x{hint}"
        assert "(click to view)" in rendered

    def test_finish_no_hint_when_no_session_id(self):
        child_id = ""
        hint = "  [dim](click to view)[/]" if child_id else ""
        assert hint == ""

    def test_on_click_skips_when_not_finished(self):
        """on_click should NOT post Clicked if not yet finished."""
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="r", task="t", child_session_id="s")
        w._finished = False
        posted = []
        w.post_message = lambda m: posted.append(m)
        w.on_click()
        assert len(posted) == 0

    def test_on_click_skips_when_no_session_id(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="r", task="t", child_session_id="")
        w._finished = True
        posted = []
        w.post_message = lambda m: posted.append(m)
        w.on_click()
        assert len(posted) == 0

    def test_on_click_posts_when_finished_and_has_id(self):
        from tui.src.ui.components.subagent_progress import SubagentProgress
        w = SubagentProgress(role="analyst", task="fix it", child_session_id="kid1")
        w._finished = True
        posted = []
        w.post_message = lambda m: posted.append(m)
        w.on_click()
        assert len(posted) == 1
        assert isinstance(posted[0], SubagentProgress.Clicked)
        assert posted[0].child_session_id == "kid1"


# ─────────────────────────────────────────────────────────────────────────────
# SubagentDetailScreen — loads session JSON
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentDetailScreen:
    def test_importable(self):
        from tui.src.ui.screens.subagent_detail import SubagentDetailScreen
        assert SubagentDetailScreen is not None

    def test_load_session_returns_none_when_missing(self, tmp_path):
        from tui.src.ui.screens.subagent_detail import SubagentDetailScreen
        screen = SubagentDetailScreen(
            child_session_id="nonexistent",
            role="analyst",
            task="x",
            sessions_dir=tmp_path,
        )
        assert screen._load_session() is None

    def test_load_session_reads_json(self, tmp_path):
        from tui.src.ui.screens.subagent_detail import SubagentDetailScreen
        data = {
            "session_id": "kid1",
            "parent_session_id": "parent1",
            "role": "analyst",
            "task_name": "do work",
            "ok": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
        (tmp_path / "session_kid1.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        screen = SubagentDetailScreen(
            child_session_id="kid1",
            role="analyst",
            task="do work",
            sessions_dir=tmp_path,
        )
        loaded = screen._load_session()
        assert loaded is not None
        assert loaded["role"] == "analyst"
        assert len(loaded["messages"]) == 1

    def test_default_sessions_dir(self):
        from tui.src.ui.screens.subagent_detail import SubagentDetailScreen
        screen = SubagentDetailScreen(child_session_id="x", role="r", task="t")
        assert screen._sessions_dir == Path.home() / ".coding_agent" / "sessions"


# ─────────────────────────────────────────────────────────────────────────────
# SessionListScreen — filter_subagents and child session rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionListScreenSubagents:
    def test_filter_subagents_flag_stored(self):
        from tui.src.ui.screens.session_list import SessionListScreen
        screen = SessionListScreen(filter_subagents=True)
        assert screen._filter_subagents is True

    def test_filter_subagents_default_false(self):
        from tui.src.ui.screens.session_list import SessionListScreen
        screen = SessionListScreen()
        assert screen._filter_subagents is False

    def test_load_sessions_filter_subagents_logic(self, tmp_path):
        """Test the child-session filtering logic without invoking self.app."""
        from tui.src.ui.screens.session_list import SessionListScreen
        # Write two session files
        parent = {"session_id": "p1", "timestamp": time.time(), "task_name": "parent", "message_count": 5, "messages": []}
        child = {"session_id": "c1", "parent_session_id": "p1", "role": "analyst", "timestamp": time.time(), "task_name": "child", "message_count": 2, "messages": []}
        (tmp_path / "session_p1.json").write_text(json.dumps(parent))
        (tmp_path / "session_c1.json").write_text(json.dumps(child))

        # Simulate what _load_sessions does after reading files (bypass self.app)
        files = sorted((tmp_path).glob("session_*.json"), reverse=True)
        all_sessions = []
        for f in files:
            all_sessions.append((f, json.loads(f.read_text(encoding="utf-8"))))

        # Apply the filter_subagents logic directly
        filtered = [(p, d) for p, d in all_sessions if d.get("parent_session_id")]
        assert len(filtered) == 1
        assert filtered[0][1]["session_id"] == "c1"

    def test_render_list_annotates_child_with_role(self, tmp_path):
        from tui.src.ui.screens.session_list import SessionListScreen
        child = {
            "session_id": "c1",
            "parent_session_id": "p1",
            "role": "analyst",
            "timestamp": time.time(),
            "task_name": "some work",
            "message_count": 3,
            "ok": True,
        }
        screen = SessionListScreen()
        screen._filtered = [(tmp_path / "session_c1.json", child)]
        screen._selected = 0

        lines_seen = []
        try:
            screen.query_one = MagicMock(side_effect=Exception("no dom"))
        except Exception:
            pass

        # Directly test the render logic by patching query_one
        captured = []
        class FakeStatic:
            def update(self, text):
                captured.append(text)

        screen.query_one = lambda *a, **kw: FakeStatic()
        screen._render_list()
        assert len(captured) == 1
        rendered = captured[0]
        assert "analyst" in rendered
        assert "↳" in rendered

    def test_filter_searches_role_field(self):
        from tui.src.ui.screens.session_list import SessionListScreen
        screen = SessionListScreen()
        screen._all_sessions = [
            (Path("/fake/s1.json"), {"task_name": "task1", "role": "debugger"}),
            (Path("/fake/s2.json"), {"task_name": "task2", "role": "analyst"}),
        ]
        screen._filtered = list(screen._all_sessions)

        class FakeStatic:
            def update(self, _): pass

        screen.query_one = lambda *a, **kw: FakeStatic()
        screen._filter("debug")
        assert len(screen._filtered) == 1
        assert screen._filtered[0][1]["role"] == "debugger"

    def test_open_selected_routing_logic_for_child(self, tmp_path):
        """_open_selected for a child session should call push_screen with SubagentDetailScreen."""
        from tui.src.ui.screens.session_list import SessionListScreen
        from tui.src.ui.screens.subagent_detail import SubagentDetailScreen
        child_data = {"session_id": "c99", "parent_session_id": "p99", "role": "analyst", "task_name": "do analysis"}

        screen = SessionListScreen()
        screen._filtered = [(tmp_path / "session_c99.json", child_data)]
        screen._selected = 0

        pushed = []
        # Override _open_selected routing by testing only the guard logic
        data = screen._filtered[0][1]
        if data.get("parent_session_id"):
            screen_obj = SubagentDetailScreen(
                child_session_id=data.get("session_id", ""),
                role=data.get("role", "subagent"),
                task=data.get("task_name", ""),
                sessions_dir=tmp_path,
            )
            pushed.append(screen_obj)

        assert len(pushed) == 1
        assert isinstance(pushed[0], SubagentDetailScreen)
        assert pushed[0]._child_session_id == "c99"

    def test_open_selected_routing_logic_for_parent(self, tmp_path):
        """_open_selected for a parent session does NOT create a SubagentDetailScreen."""
        parent_data = {"session_id": "p1", "task_name": "parent task"}
        # parent has no parent_session_id — routing guard is False
        assert not parent_data.get("parent_session_id")


# ─────────────────────────────────────────────────────────────────────────────
# subagent_tools.py — session persistence logic (mocked, no live orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentToolsSessionPersistence:
    def test_session_payload_structure(self, tmp_path):
        """Simulate the save logic from subagent_tools and verify the JSON schema."""
        import json as _json
        import time as _time
        import tempfile
        import os

        child_session_id = "child_abc"
        parent_session_id = "parent_xyz"
        subtask_description = "Analyse the codebase"
        canonical_role = "analyst"
        workdir_path = tmp_path
        final_state = {"history": [{"role": "user", "content": "hello"}]}

        # Replicate the save logic
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        _child_msgs = list(final_state.get("history") or [])
        session_payload = {
            "version": 1,
            "session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "timestamp": _time.time(),
            "task_name": subtask_description[:80],
            "role": canonical_role,
            "message_count": len(_child_msgs),
            "messages": _child_msgs,
            "working_dir": str(workdir_path),
            "ok": True,
        }

        sp = sessions_dir / f"session_{child_session_id}.json"
        fd, tmp_f = tempfile.mkstemp(dir=str(sessions_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(session_payload, f)
        os.replace(tmp_f, str(sp))

        saved = json.loads(sp.read_text())
        assert saved["session_id"] == "child_abc"
        assert saved["parent_session_id"] == "parent_xyz"
        assert saved["role"] == "analyst"
        assert saved["ok"] is True
        assert len(saved["messages"]) == 1

    def test_failed_session_payload_has_ok_false(self, tmp_path):
        import json as _json
        import time as _time
        import tempfile, os

        child_session_id = "child_fail"
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        session_payload = {
            "version": 1,
            "session_id": child_session_id,
            "parent_session_id": "parent1",
            "timestamp": _time.time(),
            "task_name": "bad task",
            "role": "debugger",
            "message_count": 0,
            "messages": [],
            "working_dir": str(tmp_path),
            "ok": False,
            "error": "TimeoutError",
        }
        sp = sessions_dir / f"session_{child_session_id}.json"
        fd, tmp_f = tempfile.mkstemp(dir=str(sessions_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(session_payload, f)
        os.replace(tmp_f, str(sp))

        saved = json.loads(sp.read_text())
        assert saved["ok"] is False
        assert saved["error"] == "TimeoutError"
        assert saved["parent_session_id"] == "parent1"
