from src.tools.system_tools import grep, get_git_diff, summarize_structure
import subprocess


def test_grep_tool(tmp_path):
    # Setup files
    (tmp_path / "file1.txt").write_text("hello world\nthis is a test\n")
    (tmp_path / "file2.txt").write_text("no match here\n")

    # Test valid grep
    res = grep("world", path=".", workdir=tmp_path)
    assert "world" in str(res)

    # Test no match
    res = grep("nonexistent", path=".", workdir=tmp_path)
    assert "not found" in str(res).lower()

    # Test error (e.g. invalid path)
    res = grep("world", path="nonexistent_dir", workdir=tmp_path)
    assert "error" in res or "not found" in str(res).lower()


def test_summarize_structure(tmp_path):
    # Setup some structure
    (tmp_path / "dir1").mkdir()
    (tmp_path / "dir1" / "file.txt").write_text("hello")

    res = summarize_structure(workdir=tmp_path)
    assert "dir1" in res.get("top", []) or res.get("file_count", 0) >= 1


def test_git_diff(monkeypatch):
    # Mock subprocess.run
    def mock_run(*args, **kwargs):
        class MockProcess:
            returncode = 0
            stdout = "diff output"

        return MockProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)
    res = get_git_diff()
    assert res.get("diff") == "diff output"

    def mock_run_fail(*args, **kwargs):
        class MockProcess:
            returncode = 1
            stderr = "not a git repo"

        return MockProcess()

    monkeypatch.setattr(subprocess, "run", mock_run_fail)
    res = get_git_diff()
    assert "error" in res

    def mock_run_notfound(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", mock_run_notfound)
    res = get_git_diff()
    assert "error" in res
