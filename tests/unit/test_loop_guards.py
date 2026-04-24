"""
tests/unit/test_loop_guards.py — Unit tests for ORCH-01/ORCH-02: loop_guards.py

Covers:
- check_read_before_write: pass when not a modifying tool, pass for new files,
  block existing unread files, pass when file is in verified_reads / files_read /
  session_read_files.
- check_cooldown: pass for non-cooldown tools, pass when enough gap, block when
  too recent, uses correct primary_arg key variants (name, query, pattern, path).
- check_doom_loop: no loop when buffer short, no loop on varied fingerprints,
  detect loop at threshold, ALLOW policy bypasses block, event_bus published on ASK.
- Constants exported at expected values.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch


from src.core.orchestration.loop_guards import (
    COOLDOWN_GAP,
    COOLDOWN_READ_TOOLS,
    DOOM_LOOP_THRESHOLD,
    MODIFYING_TOOLS,
    RECENT_CALLS_WINDOW,
    check_cooldown,
    check_doom_loop,
    check_read_before_write,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(**kwargs) -> Dict[str, Any]:
    """Minimal AgentState dict for tests."""
    base: Dict[str, Any] = {
        "files_read": {},
        "verified_reads": [],
        "recent_tool_calls": [],
        "tool_last_used": {},
        "tool_call_count": 0,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_modifying_tools_contains_expected(self):
        for name in (
            "edit_file",
            "write_file",
            "delete_file",
            "apply_patch",
            "manage_todo",
        ):
            assert name in MODIFYING_TOOLS, f"{name!r} missing from MODIFYING_TOOLS"

    def test_cooldown_read_tools_contains_expected(self):
        for name in ("read_file", "grep", "search_code", "glob", "find_symbol"):
            assert name in COOLDOWN_READ_TOOLS, (
                f"{name!r} missing from COOLDOWN_READ_TOOLS"
            )

    def test_cooldown_gap_value(self):
        assert COOLDOWN_GAP == 3

    def test_doom_loop_threshold_value(self):
        assert DOOM_LOOP_THRESHOLD == 3

    def test_recent_calls_window_value(self):
        assert RECENT_CALLS_WINDOW == 10


# ---------------------------------------------------------------------------
# check_read_before_write
# ---------------------------------------------------------------------------


class TestCheckReadBeforeWrite:
    def test_passes_for_non_modifying_tool(self):
        result = check_read_before_write("read_file", "some/path.py", _state(), "/tmp")
        assert result is None

    def test_passes_when_no_path_arg(self):
        result = check_read_before_write("edit_file", None, _state(), "/tmp")
        assert result is None

    def test_passes_for_new_file(self):
        """A path that does not exist on disk should not require a prior read."""
        result = check_read_before_write(
            "edit_file", "nonexistent_file_xyz_123.py", _state(), "/tmp"
        )
        assert result is None

    def test_blocks_existing_unread_file(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("# existing content\n")
        state = _state()
        result = check_read_before_write("edit_file", str(target), state, str(tmp_path))
        assert result is not None
        assert result["last_result"]["ok"] is False
        assert "before writing to it" in result["last_result"]["error"]
        # History entry should be JSON-encoded tool_execution_result
        history = result["history"]
        assert len(history) == 1
        payload = json.loads(history[0]["content"])
        assert "tool_execution_result" in payload

    def test_passes_when_file_in_verified_reads(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("# existing\n")
        resolved = str(target.resolve())
        state = _state(verified_reads=[resolved])
        result = check_read_before_write(
            "write_file", str(target), state, str(tmp_path)
        )
        assert result is None

    def test_passes_when_file_in_files_read(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("# existing\n")
        resolved = str(target.resolve())
        state = _state(files_read={resolved: True})
        result = check_read_before_write("edit_file", str(target), state, str(tmp_path))
        assert result is None

    def test_passes_when_file_in_session_read_files(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("# existing\n")
        resolved = str(target.resolve())
        state = _state()
        result = check_read_before_write(
            "delete_file",
            str(target),
            state,
            str(tmp_path),
            session_read_files={resolved},
        )
        assert result is None

    def test_next_action_is_none_in_error(self, tmp_path):
        target = tmp_path / "f.py"
        target.write_text("x = 1\n")
        result = check_read_before_write(
            "edit_file", str(target), _state(), str(tmp_path)
        )
        assert result is not None
        assert result["next_action"] is None

    def test_manage_todo_is_in_modifying_tools(self, tmp_path):
        """manage_todo must be checked too (TS-5 regression guard)."""
        target = tmp_path / "TODO.md"
        target.write_text("# tasks\n")
        result = check_read_before_write(
            "manage_todo", str(target), _state(), str(tmp_path)
        )
        assert result is not None


# ---------------------------------------------------------------------------
# check_cooldown
# ---------------------------------------------------------------------------


class TestCheckCooldown:
    def test_passes_for_non_cooldown_tool(self):
        result = check_cooldown("bash", {}, _state())
        assert result is None

    def test_passes_first_call(self):
        """If the tool has never been called, it has no cooldown entry."""
        result = check_cooldown("read_file", {"path": "foo.py"}, _state())
        assert result is None

    def test_passes_when_gap_is_sufficient(self):
        state = _state(
            tool_last_used={"read_file:foo.py": 0},
            tool_call_count=COOLDOWN_GAP + 1,
        )
        result = check_cooldown("read_file", {"path": "foo.py"}, state)
        assert result is None

    def test_blocks_when_gap_is_too_small(self):
        state = _state(
            tool_last_used={"read_file:foo.py": 0},
            tool_call_count=COOLDOWN_GAP - 1,
        )
        result = check_cooldown("read_file", {"path": "foo.py"}, state)
        assert result is not None
        assert result["last_result"]["ok"] is False
        assert "already in context" in result["last_result"]["error"]

    def test_cooldown_key_uses_name_arg(self):
        """find_symbol uses 'name' as the primary argument."""
        state = _state(
            tool_last_used={"find_symbol:MyClass": 0},
            tool_call_count=1,
        )
        result = check_cooldown("find_symbol", {"name": "MyClass"}, state)
        assert result is not None  # within cooldown

    def test_cooldown_key_uses_query_arg(self):
        """search_code uses 'query' as the primary argument."""
        state = _state(
            tool_last_used={"search_code:def foo": 0},
            tool_call_count=1,
        )
        result = check_cooldown("search_code", {"query": "def foo"}, state)
        assert result is not None

    def test_cooldown_key_uses_pattern_arg(self):
        """grep uses 'pattern' as the primary argument."""
        state = _state(
            tool_last_used={"grep:TODO": 0},
            tool_call_count=1,
        )
        result = check_cooldown("grep", {"pattern": "TODO"}, state)
        assert result is not None

    def test_blocked_result_increments_tool_call_count(self):
        state = _state(
            tool_last_used={"read_file:bar.py": 5},
            tool_call_count=6,  # gap = 1, which is < COOLDOWN_GAP
        )
        result = check_cooldown("read_file", {"path": "bar.py"}, state)
        assert result is not None
        assert result["tool_call_count"] == 7

    def test_next_action_is_none_when_blocked(self):
        state = _state(
            tool_last_used={"glob:src/**": 0},
            tool_call_count=1,
        )
        result = check_cooldown("glob", {"path": "src/**"}, state)
        assert result is not None
        assert result["next_action"] is None


# ---------------------------------------------------------------------------
# check_doom_loop
# ---------------------------------------------------------------------------


def _make_state_with_recent(fingerprint: str, count: int) -> Dict[str, Any]:
    """Return state with *count* identical fingerprints in recent_tool_calls."""
    return _state(recent_tool_calls=[fingerprint] * count)


class TestCheckDoomLoop:
    def test_no_loop_empty_history(self):
        err, updated = check_doom_loop("read_file", {"path": "x.py"}, _state())
        assert err is None
        assert len(updated) == 1  # fingerprint appended

    def test_no_loop_insufficient_history(self):
        fp = 'read_file:{"path": "x.py"}'
        state = _make_state_with_recent(fp, DOOM_LOOP_THRESHOLD - 2)
        err, updated = check_doom_loop("read_file", {"path": "x.py"}, state)
        assert err is None

    def test_no_loop_varied_history(self):
        """Varied fingerprints should never trigger doom loop."""
        recent = [
            'edit_file:{"path": "a.py"}',
            'read_file:{"path": "b.py"}',
        ]
        state = _state(recent_tool_calls=recent)
        err, updated = check_doom_loop("grep", {"pattern": "foo"}, state)
        assert err is None

    def test_detects_loop_at_threshold(self):
        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"read_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)
        err, updated = check_doom_loop("read_file", {"path": "x.py"}, state)
        assert err is not None
        assert "DOOM LOOP" in err["last_result"]["error"]

    def test_doom_loop_updated_recent_always_returned(self):
        """updated_recent is always returned, even when loop detected."""
        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"read_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)
        _, updated = check_doom_loop("read_file", {"path": "x.py"}, state)
        assert full_fp in updated

    def test_doom_loop_ring_buffer_capped_at_window(self):
        fp = json.dumps({}, sort_keys=True)
        full_fp = f"bash:{fp}"
        state = _make_state_with_recent(full_fp, RECENT_CALLS_WINDOW + 5)
        _, updated = check_doom_loop("bash", {}, state)
        assert len(updated) <= RECENT_CALLS_WINDOW

    def test_doom_loop_error_has_history_entry(self):
        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"edit_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)
        err, _ = check_doom_loop("edit_file", {"path": "x.py"}, state)
        assert err is not None
        history = err["history"]
        assert len(history) == 1
        payload = json.loads(history[0]["content"])
        assert "tool_execution_result" in payload

    def test_doom_loop_behavior_key_in_result(self):
        fp = json.dumps({"q": "foo"}, sort_keys=True)
        full_fp = f"search_code:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)
        err, _ = check_doom_loop("search_code", {"q": "foo"}, state)
        assert err is not None
        assert "doom_loop_behavior" in err

    def test_allow_policy_bypasses_block(self):
        """When PermissionPolicy returns ALLOW, the doom loop is permitted."""
        from src.core.orchestration.permission_policy import Behavior

        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"read_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)

        mock_policy = MagicMock()
        mock_policy.check_doom_loop.return_value = Behavior.ALLOW

        with patch(
            "src.core.orchestration.permission_policy.get_permission_policy",
            return_value=mock_policy,
        ):
            err, _ = check_doom_loop("read_file", {"path": "x.py"}, state)
        assert err is None

    def test_ask_policy_publishes_event(self):
        """When policy is ASK and event_bus provided, publish is called."""
        from src.core.orchestration.permission_policy import Behavior

        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"read_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)

        mock_policy = MagicMock()
        mock_policy.check_doom_loop.return_value = Behavior.ASK

        mock_bus = MagicMock()

        with patch(
            "src.core.orchestration.permission_policy.get_permission_policy",
            return_value=mock_policy,
        ):
            check_doom_loop("read_file", {"path": "x.py"}, state, event_bus=mock_bus)

        mock_bus.publish.assert_called_once()
        call_args = mock_bus.publish.call_args
        assert call_args[0][0] == "tool.doom_loop_detected"

    def test_policy_import_failure_still_blocks(self):
        """If policy lookup fails, the doom loop is still blocked (safe default)."""
        fp = json.dumps({"path": "x.py"}, sort_keys=True)
        full_fp = f"read_file:{fp}"
        state = _make_state_with_recent(full_fp, DOOM_LOOP_THRESHOLD - 1)

        with patch(
            "src.core.orchestration.permission_policy.get_permission_policy",
            side_effect=RuntimeError("policy unavailable"),
        ):
            err, _ = check_doom_loop("read_file", {"path": "x.py"}, state)

        assert err is not None
        assert "DOOM LOOP" in err["last_result"]["error"]
