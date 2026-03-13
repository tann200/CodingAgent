import pytest
from pathlib import Path
from src.tools.file_tools import edit_file

def test_edit_file_unified_diff(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    
    file_path = workdir / "test.txt"
    file_path.write_text("line 1\nline 2\nline 3\n")
    
    patch = """--- test.txt
+++ test.txt
@@ -1,3 +1,3 @@
 line 1
-line 2
+line 2 modified
 line 3
"""
    res = edit_file("test.txt", patch=patch, workdir=workdir)
    assert res['status'] == 'ok'
    assert file_path.read_text() == "line 1\nline 2 modified\nline 3\n"
