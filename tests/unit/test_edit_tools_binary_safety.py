import pytest
from pathlib import Path
from src.tools._edit_tools import (
    edit_file,
    edit_file_atomic,
    edit_by_line_range,
    multiedit,
)
from src.tools.patch_tools import edit_code_block
from src.tools.guardrails import mark_file_read


def test_edit_file_rejects_binary_file(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04\xff\xfe")
    mark_file_read(str(bin_file))

    res = edit_file(
        path="data.bin",
        patch="--- a/data.bin\n+++ b/data.bin\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
        workdir=tmp_path,
    )
    assert res["status"] == "error"
    assert "binary" in res["error"].lower()


def test_edit_file_atomic_rejects_binary_file(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03foo\x04\xff\xfe")
    mark_file_read(str(bin_file))

    res = edit_file_atomic(
        path="data.bin",
        old_string="foo",
        new_string="bar",
        workdir=tmp_path,
    )
    assert res["status"] == "error"
    assert "binary" in res["error"].lower()


def test_edit_by_line_range_rejects_binary_file(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03line1\nline2\n\x00")
    mark_file_read(str(bin_file))

    res = edit_by_line_range(
        path="data.bin",
        start_line=1,
        end_line=2,
        new_content="new lines\n",
        workdir=tmp_path,
    )
    assert res["status"] == "error"
    assert "binary" in res["error"].lower()


def test_multiedit_rejects_binary_file(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02target1\x03target2\x00")
    mark_file_read(str(bin_file))

    res = multiedit(
        path="data.bin",
        edits=[
            {"old_string": "target1", "new_string": "rep1"},
            {"old_string": "target2", "new_string": "rep2"},
        ],
        workdir=tmp_path,
    )
    assert res["status"] == "error"
    assert "binary" in res["error"].lower()


def test_edit_code_block_rejects_binary_file(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02block1\x03\x00")
    mark_file_read(str(bin_file))

    res = edit_code_block(
        path="data.bin",
        block_to_find="block1",
        new_block="block2",
        workdir=str(tmp_path),
    )
    assert res["status"] == "error"
    assert "binary" in res["error"].lower()
