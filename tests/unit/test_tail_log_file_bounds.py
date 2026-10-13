"""Unit tests for tail_log_file bounding and metadata.

Regression coverage for Audit §15:
- tail_log_file must reject non-positive lines (<= 0).
- tail_log_file must report accurate total_lines (not raw byte size).
- tail_log_file must report file_size_bytes accurately.
- tail_log_file must bound requested lines to _MAX_TAIL_LINES.
"""

from __future__ import annotations

from pathlib import Path

from src.tools._file_io import tail_log_file


def test_tail_log_file_rejects_negative_or_zero(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

    res_zero = tail_log_file(str(log_file), lines=0, workdir=tmp_path)
    assert res_zero.get("status") == "error"
    assert "greater than 0" in res_zero.get("error", "")

    res_neg = tail_log_file(str(log_file), lines=-5, workdir=tmp_path)
    assert res_neg.get("status") == "error"
    assert "greater than 0" in res_neg.get("error", "")


def test_tail_log_file_accurate_line_count(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    content = "first line\nsecond line\nthird line\nfourth line\nfifth line\n"
    log_file.write_text(content, encoding="utf-8")
    byte_size = len(content.encode("utf-8"))

    res = tail_log_file(str(log_file), lines=2, workdir=tmp_path)
    assert res.get("status") == "ok"
    assert res.get("lines_shown") == 2
    assert "fourth line\nfifth line\n" in res.get("content", "")
    # total_lines must be 5 (line count), NOT byte size!
    assert res.get("total_lines") == 5
    assert res.get("file_size_bytes") == byte_size
    assert res.get("total_lines") != byte_size


def test_tail_log_file_empty(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.log"
    empty_file.write_text("", encoding="utf-8")

    res = tail_log_file(str(empty_file), lines=10, workdir=tmp_path)
    assert res.get("status") == "ok"
    assert res.get("content") == ""
    assert res.get("total_lines") == 0
    assert res.get("lines_shown") == 0
    assert res.get("file_size_bytes") == 0
