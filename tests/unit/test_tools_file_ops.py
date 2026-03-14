
from src.tools import file_tools, system_tools


def test_read_file_chunk(tmp_path):
    # create a large file in the tmp workdir
    content = ("0123456789" * 20) + "\n" + ("abcdefghijklmnopqrstuvwxyz" * 10)
    f = tmp_path / "big.txt"
    f.write_text(content, encoding="utf-8")

    # read first 50 bytes via read_file_chunk using tmp_path as workdir
    res = file_tools.read_file_chunk("big.txt", offset=0, limit=50, workdir=tmp_path)
    assert res.get("status") == "ok"
    assert res.get("content") == content[:50]
    assert res.get("offset") == 0
    assert res.get("limit") == 50


def test_edit_file(tmp_path):
    # create a file and perform a surgical edit
    subdir = tmp_path / "sub"
    subdir.mkdir(parents=True, exist_ok=True)
    p = subdir / "hello.txt"
    p.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")

    patch_content = """--- sub/hello.txt
+++ sub/hello.txt
@@ -1,3 +1,3 @@
 line 1
-line 2
+line 2 modified
 line 3
"""
    res = file_tools.edit_file("sub/hello.txt", patch=patch_content, workdir=tmp_path)
    assert res.get("status") == "ok"
    text = p.read_text(encoding="utf-8")
    assert text == "line 1\nline 2 modified\nline 3\n"


def test_get_git_diff_mock(monkeypatch):
    # monkeypatch subprocess.run used in system_tools.get_git_diff to avoid depending on local git state
    class DummyProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = "fake-diff-output"
            self.stderr = ""

    def fake_run(*args, **kwargs):
        return DummyProc()

    monkeypatch.setattr(system_tools.subprocess, "run", fake_run)
    res = system_tools.get_git_diff()
    assert isinstance(res, dict)
    assert res.get("diff") == "fake-diff-output"


def test_summarize_structure(tmp_path):
    # create some files and directories
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f1.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    res = system_tools.summarize_structure(path='.', workdir=tmp_path, max_entries=10)
    assert "error" not in res  # Ensure no error was returned
    assert res.get("file_count") is not None
    assert res.get("dir_count") is not None
    assert res["file_count"] >= 2
    assert res["dir_count"] >= 1
    assert isinstance(res.get("top"), list)
