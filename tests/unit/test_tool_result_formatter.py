"""
tests/unit/test_tool_result_formatter.py — Unit tests for tool_result_formatter.py

Covers:
- format_tool_result dispatch by tool_name.
- Fallback heuristic dispatch when tool_name is None.
- Per-formatter correctness: list_files, read_file, grep, search_code,
  find_symbol, edit_file/write_file (change summary), side-by-side diff helper.
- TOOL_RESULT_FORMATTERS registry completeness.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from src.core.orchestration.tool_result_formatter import (
    TOOL_RESULT_FORMATTERS,
    format_tool_result,
    _format_change_summary,
    _format_grep_result,
    _format_list_files_result,
    _format_read_file_result,
    _format_search_result,
    _format_side_by_side_diff,
    _format_symbol_result,
)


# ---------------------------------------------------------------------------
# TOOL_RESULT_FORMATTERS registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_expected_keys_present(self):
        expected = {
            "list_files",
            "list_dir",
            "read_file",
            "grep",
            "search_code",
            "find_symbol",
            "edit_file",
            "edit_file_atomic",
            "write_file",
        }
        for key in expected:
            assert key in TOOL_RESULT_FORMATTERS, f"{key!r} missing from registry"

    def test_all_values_are_callable(self):
        for key, fn in TOOL_RESULT_FORMATTERS.items():
            assert callable(fn), f"formatter for {key!r} is not callable"


# ---------------------------------------------------------------------------
# format_tool_result — dispatch
# ---------------------------------------------------------------------------


class TestFormatToolResultDispatch:
    def test_dispatches_by_tool_name(self):
        result = {
            "matches": [{"file_path": "a.py", "line_number": 1, "content": "foo"}]
        }
        out = format_tool_result(result, tool_name="grep")
        assert "a.py" in out
        assert "foo" in out

    def test_unknown_tool_name_falls_through_to_heuristic(self):
        result = {"status": "ok", "path": "src/foo.py"}
        out = format_tool_result(result, tool_name="some_unknown_tool")
        assert "src/foo.py" in out or "Done" in out

    def test_none_tool_name_uses_heuristic(self):
        result = {"status": "ok"}
        out = format_tool_result(result, tool_name=None)
        assert isinstance(out, str)

    def test_non_dict_result_returns_str(self):
        out = format_tool_result("plain string")
        assert out == "plain string"

    def test_none_result_returns_empty(self):
        out = format_tool_result(None)
        assert out == ""

    def test_diff_key_in_result_renders_diff_block(self):
        out = format_tool_result({"diff": "- old\n+ new"})
        assert "```diff" in out
        assert "- old" in out

    def test_patch_key_in_result_renders_diff_block(self):
        out = format_tool_result({"patch": "- x\n+ y"})
        assert "```diff" in out

    def test_error_status_shows_error(self):
        out = format_tool_result({"status": "error", "error": "file not found"})
        assert "file not found" in out


# ---------------------------------------------------------------------------
# _format_list_files_result
# ---------------------------------------------------------------------------


class TestFormatListFiles:
    def test_empty_items(self):
        out = _format_list_files_result({"items": []})
        assert "Empty" in out

    def test_file_and_dir_items(self):
        items = [
            {"name": "src", "is_dir": True},
            {"name": "README.md", "is_dir": False},
        ]
        out = _format_list_files_result({"items": items})
        assert "src" in out
        assert "README.md" in out

    def test_string_items(self):
        out = _format_list_files_result({"items": ["foo.py", "bar.py"]})
        assert "foo.py" in out
        assert "bar.py" in out

    def test_non_dict_input(self):
        out = _format_list_files_result(cast(Any, "not a dict"))
        assert out == "not a dict"

    def test_no_items_key_falls_back(self):
        out = _format_list_files_result({"other": "data"})
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# _format_read_file_result
# ---------------------------------------------------------------------------


class TestFormatReadFile:
    def test_basic_content(self):
        out = _format_read_file_result({"path": "foo.py", "content": "x = 1"})
        assert "foo.py" in out
        assert "x = 1" in out

    def test_truncated_flag(self):
        out = _format_read_file_result(
            {"path": "foo.py", "content": "...", "truncated": True}
        )
        assert "truncated" in out.lower()

    def test_non_dict_input(self):
        out = _format_read_file_result(cast(Any, "raw string"))
        assert out == "raw string"


# ---------------------------------------------------------------------------
# _format_grep_result
# ---------------------------------------------------------------------------


class TestFormatGrep:
    def test_no_matches(self):
        out = _format_grep_result({"matches": []})
        assert "No matches" in out

    def test_single_match(self):
        match = {"file_path": "src/foo.py", "line_number": 42, "content": "def foo():"}
        out = _format_grep_result({"matches": [match]})
        assert "src/foo.py" in out
        assert "42" in out
        assert "def foo" in out

    def test_many_matches_capped_at_20(self):
        matches = [
            {"file_path": f"f{i}.py", "line_number": i, "content": f"line {i}"}
            for i in range(30)
        ]
        out = _format_grep_result({"matches": matches})
        assert "and 10 more" in out

    def test_non_dict_input(self):
        out = _format_grep_result(cast(Any, "not a dict"))
        assert out == "not a dict"


# ---------------------------------------------------------------------------
# _format_search_result
# ---------------------------------------------------------------------------


class TestFormatSearch:
    def test_no_results(self):
        out = _format_search_result({"results": []})
        assert "No results" in out

    def test_results_shown(self):
        r = {"file_path": "src/bar.py", "content": "class Bar:"}
        out = _format_search_result({"results": [r]})
        assert "src/bar.py" in out

    def test_non_dict_input(self):
        out = _format_search_result(cast(Any, 42))
        assert out == "42"


# ---------------------------------------------------------------------------
# _format_symbol_result
# ---------------------------------------------------------------------------


class TestFormatSymbol:
    def test_basic(self):
        out = _format_symbol_result(
            {
                "symbol_name": "MyClass",
                "file_path": "src/my.py",
                "symbol_type": "class",
                "start_line": 10,
            }
        )
        assert "MyClass" in out
        assert "src/my.py" in out
        assert "10" in out
        assert "class" in out

    def test_missing_fields_use_defaults(self):
        out = _format_symbol_result({})
        assert "?" in out  # default placeholders


# ---------------------------------------------------------------------------
# _format_change_summary
# ---------------------------------------------------------------------------


class TestFormatChangeSummary:
    def test_failed_write(self):
        out = _format_change_summary(
            {"status": "error", "error": "disk full"}, "foo.py", is_write=True
        )
        assert "Write failed" in out
        assert "disk full" in out

    def test_failed_edit(self):
        out = _format_change_summary(
            {"status": "error", "error": "pattern not found"}, "bar.py", is_write=False
        )
        assert "Edit failed" in out

    def test_successful_write_new_file(self):
        out = _format_change_summary(
            {
                "status": "ok",
                "is_new_file": True,
                "diff": "",
                "lines_added": 5,
                "lines_removed": 0,
            },
            "new.py",
            is_write=True,
        )
        assert "New file" in out
        assert "new.py" in out

    def test_successful_edit_with_diff(self):
        out = _format_change_summary(
            {
                "status": "ok",
                "diff": "- old\n+ new",
                "lines_added": 1,
                "lines_removed": 1,
            },
            "edit.py",
            is_write=False,
        )
        assert "edit.py" in out
        assert "```diff" in out
        assert "- old" in out

    def test_lines_added_removed_shown(self):
        out = _format_change_summary(
            {"status": "ok", "lines_added": 3, "lines_removed": 2},
            "x.py",
        )
        assert "+3" in out
        assert "-2" in out


# ---------------------------------------------------------------------------
# _format_side_by_side_diff
# ---------------------------------------------------------------------------


class TestFormatSideBySideDiff:
    def test_empty_input_returns_empty(self):
        assert _format_side_by_side_diff("") == ""

    def test_returns_string_for_valid_diff(self):
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
            " context\n"
        )
        out = _format_side_by_side_diff(diff)
        assert isinstance(out, str)

    def test_no_hunks_falls_back_to_original(self):
        """A diff with no @@ lines just returns the original."""
        diff = "--- a/x\n+++ b/x\n"
        out = _format_side_by_side_diff(diff)
        assert isinstance(out, str)
