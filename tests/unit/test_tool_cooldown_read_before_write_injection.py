"""
Audit Vol5 (batch 2) — security tests for bash and glob tools.

F6  — sed -i bundled-flag detection (e.g. -ni catches 'i')
F13 — glob rejects ".." traversal patterns; filters out-of-base paths
"""

# ruff: noqa: E501
from __future__ import annotations


# ---------------------------------------------------------------------------
# F6 — sed -i bundled-flag detection
# ---------------------------------------------------------------------------


class TestBashBlocksAllSedInplaceForms:
    """F6: sed -i in any form must be blocked (bare, bundled, --in-place=...)."""

    def test_bare_dash_i_blocked(self):
        from src.tools.file_tools import bash

        result = bash("sed -i 's/foo/bar/' file.txt")
        assert result.get("status") == "error"
        assert (
            "in-place" in result.get("error", "").lower()
            or "sed" in result.get("error", "").lower()
        )

    def test_bundled_ni_blocked(self):
        """sed -ni (n + i bundled) must be blocked because 'i' is present."""
        from src.tools.file_tools import bash

        result = bash("sed -ni 's/foo/bar/' file.txt")
        assert result.get("status") == "error"

    def test_bundled_rni_blocked(self):
        from src.tools.file_tools import bash

        result = bash("sed -rni 's/foo/bar/g' file.txt")
        assert result.get("status") == "error"

    def test_in_place_long_option_blocked(self):
        from src.tools.file_tools import bash

        result = bash("sed --in-place 's/a/b/' f.txt")
        assert result.get("status") == "error"

    def test_in_place_eq_blocked(self):
        from src.tools.file_tools import bash

        result = bash("sed --in-place='' 's/a/b/' f.txt")
        assert result.get("status") == "error"

    def test_sed_without_inplace_allowed(self):
        """sed without -i is a text transform; it must not be blocked by the -i check."""
        from src.tools.file_tools import bash

        result = bash("sed 's/foo/bar/' file.txt")
        assert "in-place" not in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# F13 — glob path traversal protection
# ---------------------------------------------------------------------------


class TestGlobToolRejectsPathTraversal:
    """F13: glob must reject '..' patterns and filter paths outside workdir."""

    def test_dotdot_in_pattern_rejected(self, tmp_path):
        from src.tools.file_tools import glob as glob_tool

        result = glob_tool("../../etc/passwd", workdir=tmp_path)
        assert result.get("status") == "error"
        assert ".." in result.get("error", "")

    def test_dotdot_deep_rejected(self, tmp_path):
        from src.tools.file_tools import glob as glob_tool

        result = glob_tool("sub/../../../etc/*", workdir=tmp_path)
        assert result.get("status") == "error"

    def test_valid_pattern_succeeds(self, tmp_path):
        (tmp_path / "foo.py").write_text("x=1")
        from src.tools.file_tools import glob as glob_tool

        result = glob_tool("*.py", workdir=tmp_path)
        assert result.get("status") == "ok"
        assert "foo.py" in result.get("matches", [])

    def test_recursive_pattern_succeeds(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "bar.py").write_text("y=2")
        from src.tools.file_tools import glob as glob_tool

        result = glob_tool("**/*.py", workdir=tmp_path)
        assert result.get("status") == "ok"
        assert any("bar.py" in m for m in result.get("matches", []))

    def test_result_paths_are_relative(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        from src.tools.file_tools import glob as glob_tool

        result = glob_tool("*.txt", workdir=tmp_path)
        assert result.get("status") == "ok"
        for m in result.get("matches", []):
            assert not m.startswith("/"), f"Match '{m}' must be relative"
