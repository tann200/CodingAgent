"""Unit tests for OPT-04: multi-pass fuzzy edit matcher and closest-match diagnostics."""

from __future__ import annotations

from pathlib import Path

from src.tools._edit_tools import (
    _apply_edit_match,
    _find_closest_block_hint,
    _find_edit_match,
    _fuzzy_find,
    edit_file_atomic,
    multiedit,
)
from src.tools._file_io import read_file


class TestMultiPassEditMatcher:
    def test_tighter_later_pass_beats_earlier_looser_match(self) -> None:
        # Line 2 matches only via Pass 3 (indentation-flexible);
        # Line 7 matches via Pass 1 (trailing whitespace only).
        content = (
            "def first():\n"
            "        x = 1\n"
            "        return x\n"
            "\n"
            "def second():\n"
            "    x = 1   \n"
            "    return x\n"
        )
        target = "    x = 1\n    return x"
        match, err = _find_edit_match(content, target)
        assert err is None
        assert match is not None
        assert match.strategy == "trailing_whitespace"
        assert "def second():\n" + match.matched_text in content

    def test_normalized_ambiguity_is_rejected(self) -> None:
        # Two different indentations match Pass 3 — must reject as ambiguous
        # instead of matching the first because content.count(chunk) == 1.
        content = (
            "def a():\n"
            "    val = compute()\n"
            "    return val\n"
            "\n"
            "def b():\n"
            "        val = compute()\n"
            "        return val\n"
        )
        target = "  val = compute()\n  return val"
        match, err = _find_edit_match(content, target)
        assert match is None
        assert err == "ambiguous:indentation_flexible:2"
        assert _fuzzy_find(content, target) is None

    def test_line_number_prefixes_from_read_file_are_stripped(self) -> None:
        content = "def greet(name):\n    msg = f'hi {name}'\n    return msg\n"
        numbered_old = "   2 |     msg = f'hi {name}'\n   3 |     return msg"
        numbered_new = "   2 |     msg = f'hello {name}'\n   3 |     return msg"

        match, err = _find_edit_match(content, numbered_old)
        assert err is None
        assert match is not None
        assert match.strategy == "line_prefix_stripped"
        updated = _apply_edit_match(content, match, numbered_new)
        assert updated == "def greet(name):\n    msg = f'hello {name}'\n    return msg\n"

    def test_indentation_flexible_preserves_target_indent_delta(self, tmp_path: Path) -> None:
        p = tmp_path / "calc.py"
        p.write_text(
            "class Calc:\n"
            "    def run(self, x):\n"
            "        if x > 0:\n"
            "            return x + 1\n"
            "        return 0\n",
            encoding="utf-8",
        )
        read_file(str(p), workdir=tmp_path)

        # Model supplies block dedented by 4 spaces
        res = edit_file_atomic(
            str(p),
            old_string="    if x > 0:\n        return x + 1",
            new_string="    if x > 0:\n        return x + 2",
            workdir=tmp_path,
        )
        assert res["status"] == "ok", res
        final_text = p.read_text(encoding="utf-8")
        assert "        if x > 0:\n            return x + 2\n" in final_text
        # Must remain valid Python
        compile(final_text, str(p), "exec")

    def test_closest_match_hint_attached_on_mismatch(self, tmp_path: Path) -> None:
        p = tmp_path / "app.py"
        p.write_text(
            "def alpha():\n"
            "    return 1\n"
            "\n"
            "def compute_total(items, tax_rate=0.1):\n"
            "    subtotal = sum(items)\n"
            "    return subtotal * (1 + tax_rate)\n",
            encoding="utf-8",
        )
        read_file(str(p), workdir=tmp_path)

        res = edit_file_atomic(
            str(p),
            old_string="def compute_total(items, tax_rate=0.2):\n    subtotal = sum(items)\n    return subtotal * (1 + tax)",
            new_string="def compute_total(items):\n    return sum(items)",
            workdir=tmp_path,
        )
        assert res["status"] == "error"
        assert "Closest match near lines 4-6" in res["error"]
        assert "compute_total" in res["error"]

    def test_closest_match_hint_empty_when_completely_unrelated(self) -> None:
        content = "x = 1\ny = 2\n"
        assert _find_closest_block_hint(content, "completely unrelated text block") == ""

    def test_multiedit_reports_fuzzy_ambiguity_and_closest_hint(self, tmp_path: Path) -> None:
        p = tmp_path / "multi.py"
        p.write_text(
            "def a():\n"
            "    return 10\n"
            "\n"
            "def b():\n"
            "        return 10\n",
            encoding="utf-8",
        )
        read_file(str(p), workdir=tmp_path)

        ambig = multiedit(
            str(p),
            edits=[{"old_string": "  return 10", "new_string": "  return 20"}],
            workdir=tmp_path,
        )
        assert ambig["status"] == "error"
        assert "appears 2 times" in ambig["error"]

        miss = multiedit(
            str(p),
            edits=[{"old_string": "def b():\n        return 99", "new_string": "def b():\n        return 11"}],
            workdir=tmp_path,
        )
        assert miss["status"] == "error"
        assert "Closest match near lines" in miss["error"]
