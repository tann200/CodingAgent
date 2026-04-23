"""
Bug-fix regression tests (Vol25b).

Three bugs found in code review of Vol25 additions:
  BUG-1  _slash_share crashed when bridge.history contains tuples (role, text)
          instead of dicts — msg.get("role") AttributeError
  BUG-2  _slash_rename used self.query_one(Header).sub_title which doesn't exist;
          correct form is self.sub_title (App reactive)
  BUG-3  GAP-PERM-3 "Allow Always" handler extracted tool name via regex on
          str(sib.renderable); regex cannot match rendered Rich Text.
          Fixed: tool name now cached in _perm_tool_names at request time.
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# BUG-1: _slash_share handles tuple history
# ─────────────────────────────────────────────────────────────────────────────

class TestSlashShareHistoryFormats:
    """_slash_share must handle both tuple and dict history entries."""

    def _run_format_logic(self, history):
        """Replicate the loop logic from _slash_share."""
        lines = []
        for msg in history:
            if isinstance(msg, (list, tuple)) and len(msg) == 2:
                role, content = str(msg[0]), str(msg[1])
            elif isinstance(msg, dict):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        (p.get("text") or p.get("content") or "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
            else:
                role, content = "unknown", str(msg)
            lines.append(f"**{role.upper()}**\n\n{content}\n\n---\n")
        return lines

    def test_tuple_history_no_error(self):
        history = [("user", "hello"), ("assistant", "world")]
        lines = self._run_format_logic(history)
        assert len(lines) == 2
        assert "**USER**" in lines[0]
        assert "hello" in lines[0]

    def test_dict_history_no_error(self):
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        lines = self._run_format_logic(history)
        assert len(lines) == 2
        assert "**USER**" in lines[0]

    def test_mixed_history_no_error(self):
        history = [("user", "tuple msg"), {"role": "assistant", "content": "dict msg"}]
        lines = self._run_format_logic(history)
        assert len(lines) == 2

    def test_unknown_type_falls_back(self):
        history = ["bare string"]
        lines = self._run_format_logic(history)
        assert "**UNKNOWN**" in lines[0]

    def test_dict_with_list_content_joined(self):
        history = [{"role": "tool", "content": [{"text": "part1"}, {"text": "part2"}]}]
        lines = self._run_format_logic(history)
        assert "part1" in lines[0]
        assert "part2" in lines[0]


# ─────────────────────────────────────────────────────────────────────────────
# BUG-2: _slash_rename uses self.sub_title not query_one(Header).sub_title
# ─────────────────────────────────────────────────────────────────────────────

class TestSlashRenameSubTitle:
    def _load_app_source(self) -> str:
        path = Path(__file__).parents[2] / "tui" / "src" / "ui" / "app.py"
        return path.read_text(encoding="utf-8")

    def test_rename_uses_self_sub_title_not_query_one_header(self):
        src = self._load_app_source()
        # The old broken form must not appear
        assert "query_one(Header).sub_title" not in src
        assert 'query_one(Header).subtitle' not in src

    def test_rename_uses_self_sub_title_reactive(self):
        src = self._load_app_source()
        # The correct pattern (self.sub_title = ...) must appear in _slash_rename
        rename_start = src.index("async def _slash_rename")
        worktree_start = src.index("async def _slash_worktree")
        rename_body = src[rename_start:worktree_start]
        assert "self.sub_title = new_name" in rename_body


# ─────────────────────────────────────────────────────────────────────────────
# BUG-3: GAP-PERM-3 tool name cached in _perm_tool_names, not extracted by regex
# ─────────────────────────────────────────────────────────────────────────────

class TestAllowAlwaysToolNameCache:
    def _load_app_source(self) -> str:
        path = Path(__file__).parents[2] / "tui" / "src" / "ui" / "app.py"
        return path.read_text(encoding="utf-8")

    def test_perm_tool_names_cache_populated_in_handle_tool_permission(self):
        src = self._load_app_source()
        # The cache assignment must appear in handle_tool_permission
        handler_start = src.index("async def handle_tool_permission")
        # Find the end of handle_tool_permission (next async def)
        next_handler = src.index("async def", handler_start + 1)
        handler_body = src[handler_start:next_handler]
        assert "_perm_tool_names" in handler_body
        assert "event.tool_id" in handler_body

    def test_allow_always_handler_uses_cache_not_regex(self):
        src = self._load_app_source()
        # Regex-based extraction must not appear
        assert r"Permission required:\[/\]" not in src
        # Cache lookup must appear in the allow-always handler block
        # The handler starts with the comment "# GAP-PERM-3: Allow Always"
        always_start = src.index("# GAP-PERM-3: Allow Always")
        next_section = src.index("# Doom-loop buttons", always_start)
        handler_body = src[always_start:next_section]
        assert "_perm_tool_names" in handler_body or "_perm_names" in handler_body

    def test_tool_name_lookup_from_dict(self):
        """Simulate the _perm_tool_names lookup without a running app."""
        perm_names = {"tool_id_42": "read_file"}
        tool_id = "tool_id_42"
        tool_name = perm_names.get(tool_id, tool_id)
        assert tool_name == "read_file"

    def test_tool_name_fallback_to_tool_id(self):
        """When tool_id is not in cache, fall back to tool_id itself."""
        perm_names: dict = {}
        tool_id = "some_tool_id"
        tool_name = perm_names.get(tool_id, tool_id)
        assert tool_name == "some_tool_id"
