"""tests/unit/test_file_tools_misc.py

Consolidated miscellaneous file-tool tests.  Replaces three single-test files:
  - test_edit_file_unified_diff.py  (edit_file with a unified diff patch)
  - test_tools_file_ops_extra.py    (path traversal + error paths)
  - test_system_tools_and_router.py (system_tools.grep fallback)
  - test_guilogger_concurrency.py   (GUILogger thread safety)
"""

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# edit_file — unified diff patch mode
# ---------------------------------------------------------------------------

def test_edit_file_unified_diff(tmp_path):
    from src.tools.file_tools import edit_file, read_file

    workdir = tmp_path / "work"
    workdir.mkdir()
    file_path = workdir / "test.txt"
    file_path.write_text("line 1\nline 2\nline 3\n")

    read_file("test.txt", workdir=workdir)

    patch = (
        "--- test.txt\n"
        "+++ test.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line 1\n"
        "-line 2\n"
        "+line 2 modified\n"
        " line 3\n"
    )
    res = edit_file("test.txt", patch=patch, workdir=workdir)
    assert res["status"] == "ok"
    assert file_path.read_text() == "line 1\nline 2 modified\nline 3\n"


# ---------------------------------------------------------------------------
# file_tools — path traversal + error paths
# ---------------------------------------------------------------------------

def test_file_tools_path_traversal_blocked(tmp_path):
    from src.tools.file_tools import write_file, read_file, _safe_resolve

    with pytest.raises(PermissionError):
        _safe_resolve("../outside.txt", workdir=tmp_path)

    with pytest.raises(PermissionError):
        write_file("../outside.txt", "content", workdir=tmp_path)

    with pytest.raises(PermissionError):
        read_file("../outside.txt", workdir=tmp_path)


def test_file_tools_read_nonexistent(tmp_path):
    from src.tools.file_tools import read_file
    res = read_file("nonexistent.txt", workdir=tmp_path)
    assert res.get("status") == "not_found"


def test_file_tools_edit_nonexistent(tmp_path):
    from src.tools.file_tools import edit_file
    res = edit_file("nonexistent.txt", "patch", workdir=tmp_path)
    assert res.get("status") == "not_found"


def test_file_tools_edit_bad_patch(tmp_path):
    from src.tools.file_tools import write_file, edit_file
    write_file("exists.txt", "old content\n", workdir=tmp_path)
    res = edit_file("exists.txt", "bad patch format", workdir=tmp_path)
    assert res.get("status") == "error"


def test_file_tools_write_creates_parent_dir(tmp_path):
    from src.tools.file_tools import write_file
    res = write_file("new_dir/new_file.txt", "test", workdir=tmp_path)
    assert res.get("status") == "ok"


# ---------------------------------------------------------------------------
# system_tools.grep — fallback behaviour
# ---------------------------------------------------------------------------

def test_grep_fallback(tmp_path):
    from src.tools import system_tools

    (tmp_path / "a").mkdir()
    f1 = tmp_path / "a" / "file1.txt"
    f1.write_text("hello world\nsearch-term here\n")
    f2 = tmp_path / "b.txt"
    f2.write_text("no match here\n")

    res = system_tools.grep("search-term", path=".", workdir=tmp_path)
    assert isinstance(res, dict)
    raw = (res.get("output") or {}).get("output", "")
    assert "search-term" in raw


# ---------------------------------------------------------------------------
# GUILogger — concurrent write safety
# ---------------------------------------------------------------------------

def test_guilogger_concurrency():
    """Stress test GUILogger with multiple threads; no exceptions, correct counts."""
    from src.core.logger import logger as guilogger

    guilogger.clear()
    M, K = 10, 50  # 10 threads × 50 msgs = 500 total (< default max_history 1000)

    def worker(tid):
        for j in range(K):
            guilogger.info(f"t{tid}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(M)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    time.sleep(0.05)
    logs = guilogger.get_logs(clear=False)
    cnt = sum(
        1 for e in logs
        if isinstance(e.get("message"), str) and e["message"].startswith("t")
    )
    assert cnt >= M * K, f"expected at least {M * K} logs, got {cnt}"
