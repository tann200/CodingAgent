import pytest
from pathlib import Path
from src.tools.file_tools import write_file, read_file, edit_file, _safe_resolve

def test_file_tools_exceptions(tmp_path):
    # Test path outside workdir
    with pytest.raises(PermissionError):
        _safe_resolve("../outside.txt", workdir=tmp_path)
        
    with pytest.raises(PermissionError):
        write_file("../outside.txt", "content", workdir=tmp_path)
        
    with pytest.raises(PermissionError):
        read_file("../outside.txt", workdir=tmp_path)
        
    # Read nonexistent
    res = read_file("nonexistent.txt", workdir=tmp_path)
    assert res.get("status") == "not_found"

    # Edit nonexistent
    res = edit_file("nonexistent.txt", "patch", workdir=tmp_path)
    assert res.get("status") == "not_found"

    # Edit patch fails
    write_file("exists.txt", "old content\n", workdir=tmp_path)
    res = edit_file("exists.txt", "bad patch format", workdir=tmp_path)
    assert res.get("status") == "error"

    # write_file returning ok
    res = write_file("new_dir/new_file.txt", "test", workdir=tmp_path)
    assert res.get("status") == "ok"
