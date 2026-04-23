"""tests/unit/test_inline_tool_widget.py — TASK-TUI-1

Tests for tui/src/ui/components/inline_tool.py — TOOL_RENDER_MAP and ToolRender.
Verifies correct pending/done text for each mapped tool, fallback for unknown
tools, and template variable extraction from event args.
"""

from __future__ import annotations

import pytest

from tui.src.ui.components.inline_tool import (
    FALLBACK_RENDER,
    TOOL_RENDER_MAP,
    ToolRender,
    _render,
    _resolve_arg,
)


# ---------------------------------------------------------------------------
# ToolRender: format_pending and format_done basics
# ---------------------------------------------------------------------------


def test_format_pending_simple() -> None:
    r = ToolRender(icon="->", pending="Reading ...", done="Read {path}")
    assert r.format_pending({}) == "Reading ..."


def test_format_done_extracts_path() -> None:
    r = ToolRender(icon="->", pending="Reading ...", done="Read {path}")
    assert r.format_done({"path": "src/foo.py"}) == "Read src/foo.py"


def test_label_pending_includes_icon() -> None:
    r = ToolRender(icon="->", pending="Reading ...", done="Read {path}")
    label = r.label_pending({})
    assert label.startswith("->")
    assert "Reading" in label


def test_label_done_includes_icon() -> None:
    r = ToolRender(icon="<-", pending="Writing ...", done="Write {path}")
    label = r.label_done({"path": "out.py"})
    assert label.startswith("<-")
    assert "Write out.py" in label


# ---------------------------------------------------------------------------
# Path aliases: file_path, src_path, target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,val", [
    ("path", "direct.py"),
    ("file_path", "via_file_path.py"),
    ("target", "via_target.py"),
])
def test_path_alias_resolution(key: str, val: str) -> None:
    r = TOOL_RENDER_MAP["read_file"]
    text = r.format_done({key: val})
    assert val in text


# ---------------------------------------------------------------------------
# TOOL_RENDER_MAP — every entry produces non-empty pending and done text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", list(TOOL_RENDER_MAP.keys()))
def test_every_tool_has_pending_text(tool_name: str) -> None:
    r = TOOL_RENDER_MAP[tool_name]
    # With empty args, pending should still return something (template or fallback)
    text = r.format_pending({})
    assert isinstance(text, str) and len(text) > 0


@pytest.mark.parametrize("tool_name", list(TOOL_RENDER_MAP.keys()))
def test_every_tool_has_done_text(tool_name: str) -> None:
    r = TOOL_RENDER_MAP[tool_name]
    text = r.format_done({})
    assert isinstance(text, str) and len(text) > 0


# ---------------------------------------------------------------------------
# Specific tool rendering
# ---------------------------------------------------------------------------


def test_read_file_pending() -> None:
    r = TOOL_RENDER_MAP["read_file"]
    assert "Reading" in r.format_pending({})


def test_read_file_done() -> None:
    r = TOOL_RENDER_MAP["read_file"]
    assert "Read src/main.py" in r.format_done({"path": "src/main.py"})


def test_write_file_done() -> None:
    r = TOOL_RENDER_MAP["write_file"]
    assert "Write output.txt" in r.format_done({"path": "output.txt"})


def test_bash_pending_uses_desc() -> None:
    r = TOOL_RENDER_MAP["bash"]
    text = r.format_pending({"description": "Run tests"})
    assert "Run tests" in text


def test_bash_done_uses_cmd() -> None:
    r = TOOL_RENDER_MAP["bash"]
    text = r.format_done({"command": "pytest -q"})
    assert "pytest -q" in text


def test_bash_done_alias_cmd() -> None:
    r = TOOL_RENDER_MAP["bash"]
    text = r.format_done({"bash_command": "ls -la"})
    assert "ls -la" in text


def test_glob_tool_pending_shows_pattern() -> None:
    r = TOOL_RENDER_MAP["glob_tool"]
    text = r.format_pending({"pattern": "**/*.py"})
    assert "**/*.py" in text


def test_glob_tool_done_shows_matches() -> None:
    r = TOOL_RENDER_MAP["glob_tool"]
    text = r.format_done({"pattern": "**/*.py", "count": "42"})
    assert "42" in text


def test_grep_tool_done() -> None:
    r = TOOL_RENDER_MAP["grep_tool"]
    text = r.format_done({"pattern": "TODO", "count": "7"})
    assert "TODO" in text


def test_delegate_task_done() -> None:
    r = TOOL_RENDER_MAP["delegate_task"]
    text = r.format_done({"agent": "backend", "description": "Write auth module"})
    assert "backend" in text
    assert "Write auth module" in text


def test_manage_todo_pending() -> None:
    r = TOOL_RENDER_MAP["manage_todo"]
    assert "todos" in r.format_pending({}).lower()


def test_webfetch_pending_uses_url() -> None:
    r = TOOL_RENDER_MAP["webfetch"]
    text = r.format_pending({"url": "https://example.com"})
    assert "example.com" in text


def test_memory_search_pending() -> None:
    r = TOOL_RENDER_MAP["memory_search"]
    text = r.format_pending({"query": "auth pattern"})
    assert "auth pattern" in text


# ---------------------------------------------------------------------------
# Fallback for unknown tools
# ---------------------------------------------------------------------------


def test_fallback_render_is_defined() -> None:
    assert FALLBACK_RENDER is not None
    assert FALLBACK_RENDER.icon


def test_fallback_render_pending() -> None:
    text = FALLBACK_RENDER.format_pending({"tool_name": "custom_tool"})
    assert "custom_tool" in text


def test_fallback_render_done() -> None:
    text = FALLBACK_RENDER.format_done({"tool_name": "custom_tool"})
    assert "custom_tool" in text


def test_tool_render_map_lookup_unknown_returns_none() -> None:
    assert TOOL_RENDER_MAP.get("totally_unknown_tool_xyz") is None


# ---------------------------------------------------------------------------
# Icons are ASCII-safe (no emoji characters above U+007F)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", list(TOOL_RENDER_MAP.keys()))
def test_icons_are_ascii_safe(tool_name: str) -> None:
    icon = TOOL_RENDER_MAP[tool_name].icon
    assert all(ord(c) <= 127 for c in icon), (
        f"{tool_name}: icon '{icon}' contains non-ASCII characters"
    )


# ---------------------------------------------------------------------------
# _resolve_arg: alias resolution
# ---------------------------------------------------------------------------


def test_resolve_arg_direct() -> None:
    assert _resolve_arg("path", {"path": "foo.py"}) == "foo.py"


def test_resolve_arg_alias() -> None:
    assert _resolve_arg("cmd", {"command": "echo hello"}) == "echo hello"


def test_resolve_arg_missing_returns_empty() -> None:
    assert _resolve_arg("path", {}) == ""


def test_resolve_arg_truncates_long_value() -> None:
    long_val = "x" * 100
    result = _resolve_arg("path", {"path": long_val})
    assert len(result) <= 60


# ---------------------------------------------------------------------------
# _render: template engine edge cases
# ---------------------------------------------------------------------------


def test_render_all_placeholders_filled() -> None:
    text = _render("{path} / {cmd}", {"path": "a.py", "command": "ls"}, result=None)
    assert text == "a.py / ls"


def test_render_missing_placeholder_shows_angle_brackets() -> None:
    text = _render("{path}", {}, result=None)
    assert "<path>" in text
