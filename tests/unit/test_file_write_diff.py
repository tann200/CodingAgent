from __future__ import annotations



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
    from src.tools.file_tools import write_file, read_file

    target = tmp_path / "bar.py"
    target.write_text("y = 2\n", encoding="utf-8")
    read_file("bar.py", workdir=tmp_path)
    result = write_file("bar.py", "y = 2\n", workdir=tmp_path)
    assert result.get("status") == "no_change"
    # no diff key expected for no_change
    assert "diff" in result and result.get("diff") == ""


def test_write_file_rejects_new_syntax_error_and_preserves_original(tmp_path):
    from src.tools.file_tools import write_file, read_file

    target = tmp_path / "bad.py"
    target.write_text("def ok():\n    return 1\n", encoding="utf-8")
    read_file("bad.py", workdir=tmp_path)

    result = write_file("bad.py", "def broken(:\n    return 1\n", workdir=tmp_path)

    assert result.get("status") == "error"
    assert "Pre-write verification failed" in result.get("error", "")
    assert target.read_text(encoding="utf-8") == "def ok():\n    return 1\n"
