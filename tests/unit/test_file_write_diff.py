from __future__ import annotations

from pathlib import Path


def test_write_file_returns_diff_on_change(tmp_path):
    from src.tools.file_tools import write_file

    target = tmp_path / "foo.py"
    # Ensure file does not exist yet
    if target.exists():
        target.unlink()
    result = write_file("foo.py", "x = 1\n", workdir=tmp_path)
    assert result.get("status") == "ok"
    assert "diff" in result
    assert "@@" in result["diff"]


def test_write_file_no_change_has_no_diff(tmp_path):
    from src.tools.file_tools import write_file

    target = tmp_path / "bar.py"
    target.write_text("y = 2\n", encoding="utf-8")
    result = write_file("bar.py", "y = 2\n", workdir=tmp_path)
    assert result.get("status") == "no_change"
    # no diff key expected for no_change
    assert "diff" in result and result.get("diff") == ""
